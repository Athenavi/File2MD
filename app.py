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

# 初始化Flask应用
app = Flask(__name__)

# 线程池执行器
executor = ThreadPoolExecutor(max_workers=4)

# 配置限流
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"]
)


def get_env_variable(var_name, default_value=None):
    value = os.environ.get(var_name)
    if not value and var_name in ['REDIS_HOST', 'CELERY_BROKER_URL']:
        raise ValueError(f"Missing required environment variable: {var_name}")
    return value or default_value


# 应用配置
app.config.update({
    'UPLOAD_FOLDER': get_env_variable('UPLOAD_FOLDER', 'uploads/'),
    'OUTPUT_FOLDER': get_env_variable('OUTPUT_FOLDER', 'output/'),
    'MAX_CONTENT_LENGTH': 50 * 1024 * 1024,
    'FILE_RETENTION_HOURS': int(get_env_variable('FILE_RETENTION_HOURS', '1')),
    'CONVERSION_TIMEOUT': int(get_env_variable('CONVERSION_TIMEOUT', '300')),
    'MAX_INITIAL_SIZE': int(get_env_variable('MAX_INITIAL_SIZE', '102400')),
    'CSP_POLICY': get_env_variable('CSP_POLICY', "default-src 'self'"),
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


# 日志配置
class SanitizedFileHandler(logging.FileHandler):
    def emit(self, record):
        if hasattr(record, 'path'):
            record.path = os.path.basename(record.path)
        super().emit(record)


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        SanitizedFileHandler('app.log'),
        logging.StreamHandler()
    ]
)

# 初始化SocketIO
socketio = SocketIO(app, async_mode='threading')


# Redis连接检查
def is_redis_available():
    try:
        r = redis.StrictRedis(
            host=get_env_variable('REDIS_HOST', 'localhost'),
            port=int(get_env_variable('REDIS_PORT', '6379')),
            db=0
        )
        return r.ping()
    except (redis.ConnectionError, ValueError) as e:
        logging.warning(f"Redis connection failed: {str(e)}")
        return False


# 缓存和Celery初始化
redis_available = is_redis_available()
if redis_available:
    app.config['CELERY_BROKER_URL'] = get_env_variable('CELERY_BROKER_URL', 'redis://localhost:6379/0')
    celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
    cache = Cache(app, config={
        'CACHE_TYPE': 'RedisCache',
        'CACHE_DEFAULT_TIMEOUT': 300,
        'CACHE_REDIS_URL': app.config['CELERY_BROKER_URL']
    })
else:
    cache = Cache(app, config={
        'CACHE_TYPE': 'SimpleCache',
        'CACHE_DEFAULT_TIMEOUT': 300
    })

md = MarkItDown()
file_status_queue = Queue()
upload_lock = Lock()

# 创建必要目录
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)


# 文件验证函数
def allowed_file(filename):
    """验证文件名扩展"""
    if '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in app.config['ALLOWED_MIME_TYPES']


def initial_validation(file):
    """内存中的初步验证"""
    try:
        file.seek(0)
        header = file.read(app.config['MAX_INITIAL_SIZE'])
        file.seek(0)

        if len(header) == 0:
            raise ValueError("Empty file")

        valid_signatures = {
            b'%PDF-': 'pdf',
            b'\x89PNG': 'png',
            b'\xFF\xD8\xFF': 'jpg',
            b'PK\x03\x04': ['docx', 'pptx', 'xlsx']
        }

        for sig, exts in valid_signatures.items():
            if header.startswith(sig):
                if isinstance(exts, list):
                    return True  # 具体类型由后续验证确定
                return exts
        return None
    except Exception as e:
        logging.error(f"Initial validation failed: {str(e)}")
        return None


def validate_file_type(file_path):
    """MIME类型验证"""
    try:
        import magic
        mime = magic.Magic(mime=True)
        return mime.from_file(file_path) in app.config['ALLOWED_MIME_TYPES'].values()
    except ImportError:
        logging.warning("python-magic not available, using extension validation")
        ext = file_path.split('.')[-1].lower()
        return app.config['ALLOWED_MIME_TYPES'].get(ext) is not None


def is_file_content_valid(file_path):
    """增强型文件内容验证"""
    try:
        if not validate_file_type(file_path):
            return False

        ext = file_path.split('.')[-1].lower()

        if ext in ('jpg', 'jpeg', 'png'):
            with Image.open(file_path) as img:
                img.verify()
                img.load()  # 进一步验证图像数据
            return True
        elif ext == 'pdf':
            with open(file_path, 'rb') as f:
                reader = PdfReader(f)
                return len(reader.pages) > 0 and not reader.is_encrypted
        elif ext == 'docx':
            doc = Document(file_path)
            return len(doc.paragraphs) > 0
        elif ext == 'xlsx':
            wb = openpyxl.load_workbook(file_path)
            return len(wb.sheetnames) > 0
        elif ext == 'txt':
            return os.path.getsize(file_path) > 0
        return True
    except Exception as e:
        logging.error(f"Content validation failed: {str(e)}", extra={'path': os.path.basename(file_path)})
        return False


def cleanup_file(file_path):
    """安全删除文件"""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logging.info(f"Cleaned up file: {os.path.basename(file_path)}")
    except Exception as e:
        logging.error(f"Cleanup failed: {str(e)}", extra={'path': os.path.basename(file_path)})


def handle_conversion(file_path, unique_id):
    """处理文件转换的核心逻辑"""
    try:
        logging.info(f"Starting conversion: {os.path.basename(file_path)}")
        start_time = time.time()

        result = md.convert(file_path)
        output_path = os.path.join(app.config['OUTPUT_FOLDER'], f"{unique_id}.md")

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(result.text_content)

        cache.set(unique_id, {
            'status': 'completed',
            'path': output_path,
            'timestamp': time.time()
        })

        duration = time.time() - start_time
        logging.info(f"Conversion completed in {duration:.2f}s: {os.path.basename(file_path)}")
        socketio.emit('process_complete', {
            'unique_id': unique_id,
            'url': f"/download/{unique_id}",
            'duration': duration
        })
    except TimeoutError as e:
        error_msg = f"Conversion timed out: {str(e)}"
        logging.error(error_msg)
        cache.set(unique_id, {
            'status': 'failed',
            'error': error_msg,
            'timestamp': time.time()
        })
        socketio.emit('process_complete', {
            'unique_id': unique_id,
            'error': error_msg
        })
    except Exception as e:
        error_msg = f"Conversion error: {str(e)}"
        logging.error(error_msg)
        cache.set(unique_id, {
            'status': 'failed',
            'error': error_msg,
            'timestamp': time.time()
        })
        socketio.emit('process_complete', {
            'unique_id': unique_id,
            'error': error_msg
        })
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
            cache.set(unique_id, {
                'status': 'failed',
                'error': f"Conversion failed: {str(e)}",
                'timestamp': time.time()
            })
            self.update_state(state='FAILURE', meta={'error': str(e)})
            raise


@app.route('/')
def upload_form():
    return render_template('upload.html',
                           allowed_mime_types=app.config['ALLOWED_MIME_TYPES'],
                           maxSize=app.config['MAX_CONTENT_LENGTH'])


@app.route('/download/<uuid:unique_id>')
def download_file(unique_id):
    unique_id = str(unique_id)
    file_data = cache.get(unique_id)

    if not file_data or file_data['status'] != 'completed':
        return jsonify(status='error', message='File not found'), 404

    try:
        return send_file(
            file_data['path'],
            mimetype='text/markdown',
            as_attachment=True,
            download_name=f"{unique_id}.md",
            environ=request.environ
        )
    except Exception as e:
        logging.error(f"Download failed: {str(e)}")
        return jsonify(status='error', message='File download failed'), 500


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

    # 初步验证
    valid_ext = initial_validation(file)
    if valid_ext is None:
        return jsonify(status='error', message='Invalid file signature'), 400

    unique_id = str(uuid.uuid4())
    temp_filename = f"{unique_id}.{valid_ext if isinstance(valid_ext, str) else file.filename.rsplit('.', 1)[1].lower()}"
    temp_path = os.path.join(app.config['UPLOAD_FOLDER'], temp_filename)

    try:
        file.save(temp_path)

        if not is_file_content_valid(temp_path):
            raise ValueError("Invalid file content")

        cache.set(unique_id, {
            'status': 'processing',
            'path': temp_path,
            'timestamp': time.time()
        })

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
    retention_sec = app.config['FILE_RETENTION_HOURS'] * 3600

    for folder in [app.config['UPLOAD_FOLDER'], app.config['OUTPUT_FOLDER']]:
        for f in glob.glob(os.path.join(folder, '*')):
            try:
                if (now - os.path.getmtime(f)) > retention_sec:
                    cleanup_file(f)
            except Exception as e:
                logging.error(f"Cleanup check failed: {str(e)}")


scheduler.add_job(clean_up_files, 'interval', hours=1)
scheduler.start()


@atexit.register
def shutdown():
    try:
        scheduler.shutdown()
        executor.shutdown(wait=True)
        logging.info("Service shutdown completed")
    except Exception as e:
        logging.error(f"Shutdown error: {str(e)}")


if __name__ == '__main__':
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True)
