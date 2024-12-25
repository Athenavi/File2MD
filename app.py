import atexit
import glob
import logging
import os
import time
import uuid
from threading import Lock
from queue import Queue

import dotenv
import openpyxl
import redis
from PIL import Image
from PyPDF2 import PdfReader
from apscheduler.schedulers.background import BackgroundScheduler
from celery import Celery
from docx import Document
from flask import Flask, render_template, jsonify, request
from flask_caching import Cache
from flask_socketio import SocketIO, emit
from markitdown import MarkItDown
from werkzeug.utils import secure_filename

# Load environment variables (including Redis configuration) from .env file if available
dotenv.load_dotenv()

# Flask configuration
app = Flask(__name__, static_url_path='/output', static_folder='output')


def get_env_variable(var_name, default_value):
    value = os.environ.get(var_name)
    if value is None:
        logging.warning(f"{var_name} is not set, using default: {default_value}")
    return value or default_value


app.config['UPLOAD_FOLDER'] = get_env_variable('UPLOAD_FOLDER', 'uploads/')
app.config['OUTPUT_FOLDER'] = get_env_variable('OUTPUT_FOLDER', 'output/')
app.config['ALLOWED_EXTENSIONS'] = {
    'pdf', 'docx', 'pptx', 'xlsx', 'jpg', 'jpeg', 'png', 'gif', 'wav', 'mp3', 'html', 'csv', 'json', 'xml'
}
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # max upload size 50MB

# Logging settings
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize SocketIO
socketio = SocketIO(app)


# Check if Redis is available
def is_redis_available():
    try:
        r = redis.StrictRedis(host=os.environ.get('REDIS_HOST', 'localhost'),
                              port=os.environ.get('REDIS_PORT', 6379),
                              db=0)
        r.ping()
        return True
    except redis.ConnectionError:
        return False


# Initialize Cache and Celery
if is_redis_available():
    app.config['CELERY_BROKER_URL'] = get_env_variable('CELERY_BROKER_URL', 'redis://localhost:6379/0')
    celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
    cache = Cache(app, config={'CACHE_TYPE': 'RedisCache', 'CACHE_DEFAULT_TIMEOUT': 300})
else:
    logging.warning("Redis is not available. Fallback to SimpleCache.")
    cache = Cache(app, config={'CACHE_TYPE': 'SimpleCache', 'CACHE_DEFAULT_TIMEOUT': 300})

md = MarkItDown()
file_status_queue = Queue()
upload_lock = Lock()

# Create necessary folders
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)


# Check if the file type is allowed
def allowed_file(filename):
    if not filename or not isinstance(filename, str):
        return False
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']


# Comprehensive file content validation
def is_file_content_valid(file):
    file.seek(0)

    if file.filename.endswith(('.jpg', '.jpeg', '.png')):
        try:
            with Image.open(file) as img:
                img.verify()
            return True
        except Exception:
            return False

    elif file.filename.endswith('.pdf'):
        try:
            with PdfReader(file) as reader:
                return bool(len(reader.pages) > 0)
        except Exception:
            return False

    elif file.filename.endswith('.docx'):
        try:
            with Document(file) as doc:
                return bool(doc.paragraphs)  # Ensure DOCX has paragraphs
        except Exception:
            return False

    elif file.filename.endswith('.xlsx'):
        try:
            with openpyxl.load_workbook(file) as workbook:
                return bool(workbook.sheetnames)  # Ensure XLSX has sheets
        except Exception:
            return False

    return True


def update_file_status(unique_id, status, download_url=None, error=None):
    status_entry = {'unique_id': unique_id, 'status': status, 'download_url': download_url, 'error': error}
    file_status_queue.put(status_entry)


def handle_file_conversion(file_path, unique_id):
    try:
        result = md.convert(file_path)  # Convert file
        md_file_path = os.path.join(app.config['OUTPUT_FOLDER'], f"{unique_id}.md")

        with open(md_file_path, 'w', encoding='utf-8') as f:
            f.write(result.text_content)

        update_file_status(unique_id, 'completed', md_file_path)
        socketio.emit('process_complete', {'unique_id': unique_id, 'url': md_file_path})

    except Exception as e:
        error_message = f"Error converting file '{file_path}': {str(e)}"
        logging.exception(error_message)
        update_file_status(unique_id, 'failed', error=error_message)
        socketio.emit('process_complete', {'unique_id': unique_id, 'error': error_message})


# Define Celery task only if Redis is available
if is_redis_available():
    @celery.task(bind=True)
    def convert_file(self, file_path, unique_id):
        """Celery task for converting files."""
        try:
            handle_file_conversion(file_path, unique_id)
        except Exception as e:
            error_message = f"Celery task failed for '{file_path}': {str(e)}"
            logging.error(error_message)
            self.update_state(state='FAILURE', meta={'error': str(e)})
            update_file_status(unique_id, 'failed', error=error_message)
            socketio.emit('process_complete', {'unique_id': unique_id, 'error': error_message})

# Set up background scheduler for clean-up
scheduler = BackgroundScheduler()


def clean_up_files():
    current_time = time.time()
    expiration_time = 3600  # 清理过期文件的时间

    for file in glob.glob(os.path.join(app.config['OUTPUT_FOLDER'], '*.md')):
        file_last_access_time = os.path.getatime(file)
        if current_time - file_last_access_time > expiration_time:
            os.remove(file)
            logging.info(f"Deleted expired file: {file}")
        else:
            logging.info(f"File in use or not expired, skipping: {file}")


scheduler.add_job(clean_up_files, 'interval', hours=1)
scheduler.start()


@app.route('/')
def upload_form():
    return render_template('upload.html', allowedType=app.config['ALLOWED_EXTENSIONS'],
                           maxSize=app.config['MAX_CONTENT_LENGTH'])


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify(status='error', message='No file part'), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify(status='error', message='No selected file'), 400

    with upload_lock:
        unique_id = str(uuid.uuid4())
        if file and allowed_file(file.filename):
            unique_filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)

            if not is_file_content_valid(file):
                return jsonify(status='error', message='Invalid file content'), 400

            file.save(file_path)
            update_file_status(unique_id, 'processing', None)

            try:
                if is_redis_available():
                    convert_file.apply_async(args=(file_path, unique_id))
                else:
                    handle_file_conversion(file_path, unique_id)
            except Exception as e:
                logging.error(f"Error during file processing: {str(e)}")
                return jsonify(status='error', message=f'File processing error: {str(e)}'), 500

            return jsonify(status='success', unique_id=unique_id)

    return jsonify(status='error', message='Invalid file type'), 400


@socketio.on('connect')
def handle_connect():
    emit('response', {'message': 'Connected to server.'})


# Register for exit
atexit.register(lambda: scheduler.shutdown())  # Ensure scheduler ends when app exits

if __name__ == '__main__':
    logging.info("Starting application...")
    socketio.run(app, debug=True)