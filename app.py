import atexit
import glob
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Lock

import dotenv
import magic
import openpyxl
import redis
from PIL import Image
from PyPDF2 import PdfReader
from apscheduler.schedulers.background import BackgroundScheduler
from celery import Celery
from docx import Document
from flask import Flask, render_template, jsonify, request
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO
from markitdown import MarkItDown
from werkzeug.utils import send_file

# 初始化环境变量
dotenv.load_dotenv()

# Flask应用配置
app = Flask(__name__)

# 线程池执行器
executor = ThreadPoolExecutor(max_workers=4)


# 安全头设置
@app.after_request
def add_security_headers(response):
    security_headers = {
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'X-XSS-Protection': '1; mode=block'
    }
    for header, value in security_headers.items():
        response.headers[header] = value
    return response


# 配置限流
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"]
)


def get_env_variable(var_name, default_value):
    return os.environ.get(var_name) or default_value


app.config.update({
    'UPLOAD_FOLDER': get_env_variable('UPLOAD_FOLDER', 'uploads/'),
    'OUTPUT_FOLDER': get_env_variable('OUTPUT_FOLDER', 'output/'),
    'MAX_CONTENT_LENGTH': 50 * 1024 * 1024,
    'ALLOWED_MIME_TYPES': {
        'pdf': 'application/pdf',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'png': 'image/png',
        'gif': 'image/gif',
        'txt': 'text/plain',
        'md': 'text/markdown'
    }
})


# 日志安全配置
class SanitizedFileHandler(logging.FileHandler):
    def emit(self, record):
        if hasattr(record, 'path'):
            record.path = os.path.basename(record.path)
        super().emit(record)


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[SanitizedFileHandler('app.log')]
)

# 初始化SocketIO
socketio = SocketIO(app, async_mode='threading')


# Redis连接检查
def is_redis_available():
    try:
        r = redis.StrictRedis(
            host=os.environ.get('REDIS_HOST', 'localhost'),
            port=int(os.environ.get('REDIS_PORT', 6379)),
            db=0
        )
        return r.ping()
    except redis.ConnectionError:
        return False


# 缓存和Celery初始化
redis_available = is_redis_available()
if redis_available:
    app.config['CELERY_BROKER_URL'] = get_env_variable('CELERY_BROKER_URL', 'redis://localhost:6379/0')
    celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
    cache = Cache(app, config={'CACHE_TYPE': 'RedisCache', 'CACHE_DEFAULT_TIMEOUT': 300})
else:
    cache = Cache(app, config={'CACHE_TYPE': 'SimpleCache', 'CACHE_DEFAULT_TIMEOUT': 300})

md = MarkItDown()
file_status_queue = Queue()
upload_lock = Lock()

# 创建必要目录
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)


def allowed_file(filename):
    """验证文件名和真实类型"""
    if '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in app.config['ALLOWED_MIME_TYPES']


def validate_file_type(file_path):
    """使用magic进行MIME类型验证"""
    mime = magic.Magic(mime=True)
    detected_type = mime.from_file(file_path)
    return detected_type in app.config['ALLOWED_MIME_TYPES'].values()


def is_file_content_valid(file_path):
    """增强型文件内容验证"""
    try:
        if not validate_file_type(file_path):
            return False

        if file_path.lower().endswith(('.jpg', '.jpeg', '.png')):
            with Image.open(file_path) as img:
                img.verify()
            return True
        elif file_path.lower().endswith('.pdf'):
            with open(file_path, 'rb') as f:
                return len(PdfReader(f).pages) > 0
        elif file_path.lower().endswith('.docx'):
            return bool(Document(file_path).paragraphs)
        elif file_path.lower().endswith('.xlsx'):
            return bool(openpyxl.load_workbook(file_path).sheetnames)
        return True
    except Exception as e:
        logging.error(f"File validation failed: {str(e)}", extra={'path': file_path})
        return False


def cleanup_file(file_path):
    """安全删除文件"""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logging.info(f"Cleaned up file: {os.path.basename(file_path)}")
    except Exception as e:
        logging.error(f"Cleanup failed: {str(e)}", extra={'path': file_path})


def handle_conversion(file_path, unique_id):
    """处理文件转换的核心逻辑"""
    try:
        result = md.convert(file_path)
        output_path = os.path.join(app.config['OUTPUT_FOLDER'], f"{unique_id}.md")

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(result.text_content)

        cache.set(unique_id, {'status': 'completed', 'path': output_path})
        socketio.emit('process_complete', {'unique_id': unique_id, 'url': f"/download/{unique_id}"})
    except Exception as e:
        error_msg = f"Conversion error: {str(e)}"
        cache.set(unique_id, {'status': 'failed', 'error': error_msg})
        socketio.emit('process_complete', {'unique_id': unique_id, 'error': error_msg})
    finally:
        cleanup_file(file_path)


if redis_available:
    @celery.task(bind=True)
    def async_conversion_task(self, file_path, unique_id):
        """Celery异步任务"""
        try:
            handle_conversion(file_path, unique_id)
            return {'status': 'completed'}
        except Exception as e:
            self.update_state(state='FAILURE', meta={'error': str(e)})
            return {'status': 'failed', 'error': str(e)}


@app.route('/')
def upload_form():
    return render_template('upload.html', allowed_mime_types=app.config['ALLOWED_MIME_TYPES'],
                           maxSize=app.config['MAX_CONTENT_LENGTH'])


@app.route('/download/<uuid:unique_id>')
def download_file(unique_id):
    unique_id = str(unique_id)
    file_data = cache.get(unique_id)

    if not file_data or file_data['status'] != 'completed':
        return jsonify(status='error', message='File not found'), 404

    return send_file(
        file_data['path'],
        mimetype='text/markdown',
        as_attachment=True,
        download_name=f"{unique_id}.md",
        environ=request.environ
    )


@app.route('/upload', methods=['POST'])
@limiter.limit("5/minute")
def upload_file():
    if 'file' not in request.files:
        return jsonify(status='error', message='No file uploaded'), 400

    file = request.files['file']
    if not file or file.filename == '':
        return jsonify(status='error', message='Invalid file'), 400

    if not allowed_file(file.filename):
        return jsonify(status='error', message='File type not allowed'), 400

    unique_id = str(uuid.uuid4())
    file_ext = file.filename.rsplit('.', 1)[1].lower()
    temp_filename = f"{unique_id}.{file_ext}"
    temp_path = os.path.join(app.config['UPLOAD_FOLDER'], temp_filename)

    try:
        file.save(temp_path)
        if not is_file_content_valid(temp_path):
            raise ValueError("Invalid file content")

        cache.set(unique_id, {'status': 'processing', 'path': temp_path})

        if redis_available:
            async_conversion_task.delay(temp_path, unique_id)
        else:
            executor.submit(handle_conversion, temp_path, unique_id)

        return jsonify(status='success', unique_id=unique_id)

    except Exception as e:
        cleanup_file(temp_path)
        logging.error(f"Upload failed: {str(e)}")
        return jsonify(status='error', message='File processing failed'), 500


# 后台清理任务
scheduler = BackgroundScheduler()


def clean_up_files():
    now = time.time()
    for folder in [app.config['UPLOAD_FOLDER'], app.config['OUTPUT_FOLDER']]:
        for f in glob.glob(os.path.join(folder, '*')):
            if os.path.getctime(f) < now - 3600:
                cleanup_file(f)


scheduler.add_job(clean_up_files, 'interval', hours=1)
scheduler.start()


@atexit.register
def shutdown():
    scheduler.shutdown()
    executor.shutdown(wait=False)


if __name__ == '__main__':
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True)
