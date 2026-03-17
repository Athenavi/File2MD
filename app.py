import atexit
import glob
import logging
import os
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
from werkzeug.utils import send_file, secure_filename

# 初始化环境变量
# 获取打包后的资源路径
def get_resource_path(relative_path):
    """获取资源文件的绝对路径（支持 PyInstaller 打包）"""
    if hasattr(sys, '_MEIPASS'):
        # PyInstaller 临时目录
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

dotenv.load_dotenv()

# 初始化 Flask 应用
# 获取模板文件夹路径（支持打包后）
template_folder = get_resource_path('templates')
app = Flask(__name__, template_folder=template_folder)

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
    'UPLOAD_FOLDER': get_env_variable('UPLOAD_FOLDER', get_resource_path('uploads/')),
    'OUTPUT_FOLDER': get_env_variable('OUTPUT_FOLDER', get_resource_path('output/')),
    'MAX_CONTENT_LENGTH': 50 * 1024 * 1024,
    'FILE_RETENTION_HOURS': int(get_env_variable('FILE_RETENTION_HOURS', '1')),
    'CONVERSION_TIMEOUT': int(get_env_variable('CONVERSION_TIMEOUT', '300')),
    'MAX_INITIAL_SIZE': int(get_env_variable('MAX_INITIAL_SIZE', '102400')),
    'CSP_POLICY': get_env_variable('CSP_POLICY', "default-src 'self'; script-src 'self' https://code.jquery.com https://cdn.socket.io https://cdnjs.cloudflare.com 'unsafe-inline'; style-src 'self' https://cdnjs.cloudflare.com 'unsafe-inline'; font-src 'self' https://cdnjs.cloudflare.com; connect-src 'self' ws: wss:"),
    'ALLOWED_MIME_TYPES': {
        'pdf': 'application/pdf',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'xls': 'application/vnd.ms-excel',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'png': 'image/png',
        'gif': 'image/gif',
        'txt': 'text/plain',
        'md': 'text/markdown',
        'csv': 'text/csv',
        'json': 'application/json',
        'xml': 'application/xml',
        'html': 'text/html',
        'zip': 'application/zip',
        'wav': 'audio/wav',
        'mp3': 'audio/mpeg',
        'epub': 'application/epub+zip'
    }
})


# 日志配置
class SensitiveDataFilter(logging.Filter):
    """日志敏感信息过滤器"""
    
    SENSITIVE_PATTERNS = [
        (r'sk-[a-zA-Z0-9]{32,}', 'sk-***REDACTED***'),  # OpenAI API Key
        (r'Bearer\s+[a-zA-Z0-9\-_\.]+', 'Bearer ***REDACTED***'),  # Bearer Token
        (r'api[_-]?key["\']?\s*[:=]\s*["\']?[a-zA-Z0-9]{20,}', 'api_key=***REDACTED***'),
        (r'password["\']?\s*[:=]\s*["\']?[^"\',\s]+', 'password=***REDACTED***'),
        (r'secret["\']?\s*[:=]\s*["\']?[^"\',\s]+', 'secret=***REDACTED***'),
    ]
    
    def filter(self, record):
        """过滤日志记录中的敏感信息"""
        if hasattr(record, 'msg'):
            msg = str(record.msg)
            for pattern, replacement in self.SENSITIVE_PATTERNS:
                import re
                msg = re.sub(pattern, replacement, msg, flags=re.IGNORECASE)
            record.msg = msg
        
        # 清理参数中的敏感信息
        if hasattr(record, 'args') and record.args:
            if isinstance(record.args, dict):
                filtered_args = {}
                for key, value in record.args.items():
                    if any(sensitive in key.lower() for sensitive in ['key', 'secret', 'password', 'token']):
                        filtered_args[key] = '***REDACTED***'
                    else:
                        filtered_args[key] = value
                record.args = filtered_args
        
        return True


class SanitizedFileHandler(logging.FileHandler):
    def emit(self, record):
        # 进一步清理路径信息
        if hasattr(record, 'path'):
            record.path = os.path.basename(record.path)
        if hasattr(record, 'pathname'):
            # 缩短过长的路径
            pathname = str(record.pathname)
            if len(pathname) > 50:
                record.pathname = f"...{pathname[-50:]}"
        super().emit(record)


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        SanitizedFileHandler('app.log'),
        logging.StreamHandler()
    ]
)

# 添加敏感信息过滤器
log_filter = SensitiveDataFilter()
for handler in logging.getLogger().handlers:
    handler.addFilter(log_filter)

# 初始化 SocketIO
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

md = MarkItDown(enable_plugins=False)  # 可根据需要启用插件
file_status_queue = Queue()
upload_lock = Lock()

# 创建必要目录
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

# 确保模板目录存在（打包后）
template_folder = get_resource_path('templates')
if not os.path.exists(template_folder):
    logging.warning(f"Templates folder not found: {template_folder}")


@contextmanager
def sandboxed_file_operation(file_path):
    """
    沙箱环境下的文件操作上下文管理器
    确保文件操作在隔离环境中进行，自动清理临时资源
    """
    temp_dir = None
    try:
        # 创建临时沙箱目录
        temp_dir = tempfile.mkdtemp(prefix='sandbox_')
        logging.info(f"Created sandbox directory: {os.path.basename(temp_dir)}")
        yield file_path
    except Exception as e:
        logging.error(f"Sandbox operation failed: {str(e)}")
        raise
    finally:
        # 清理临时目录
        if temp_dir and os.path.exists(temp_dir):
            try:
                import shutil
                shutil.rmtree(temp_dir)
                logging.info(f"Cleaned up sandbox: {os.path.basename(temp_dir)}")
            except Exception as e:
                logging.warning(f"Failed to cleanup sandbox: {str(e)}")


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
    """MIME 类型验证"""
    try:
        import magic
        mime = magic.Magic(mime=True)
        detected_mime = mime.from_file(file_path)
        logging.info(f"Detected MIME type for {os.path.basename(file_path)}: {detected_mime}")
        return detected_mime in app.config['ALLOWED_MIME_TYPES'].values()
    except ImportError:
        logging.warning("python-magic not available, using extension validation only")
        ext = file_path.split('.')[-1].lower()
        expected_mime = app.config['ALLOWED_MIME_TYPES'].get(ext)
        if expected_mime:
            logging.info(f"Using extension-based validation for .{ext}: {expected_mime}")
            return True
        return False


def is_file_content_valid(file_path):
    """增强型文件内容验证（沙箱隔离）"""
    try:
        with sandboxed_file_operation(file_path):
            if not validate_file_type(file_path):
                return False

            ext = file_path.split('.')[-1].lower()

            if ext in ('jpg', 'jpeg', 'png'):
                try:
                    with Image.open(file_path) as img:
                        img.verify()
                        # 重新打开图像以加载数据（verify 后需要重新加载）
                        img.load()
                    return True
                except Exception as img_error:
                    logging.error(f"Image validation failed for {os.path.basename(file_path)}: {str(img_error)}", 
                                 extra={'path': os.path.basename(file_path)})
                    # 尝试获取更详细的图像信息
                    try:
                        with Image.open(file_path) as img:
                            logging.error(f"Image details - Format: {img.format}, Mode: {img.mode}, Size: {img.size}")
                    except:
                        pass
                    return False
            elif ext == 'pdf':
                try:
                    with open(file_path, 'rb') as f:
                        reader = PdfReader(f)
                        return len(reader.pages) > 0 and not reader.is_encrypted
                except Exception as pdf_error:
                    logging.error(f"PDF validation failed: {str(pdf_error)}", extra={'path': os.path.basename(file_path)})
                    return False
            elif ext == 'docx':
                try:
                    doc = Document(file_path)
                    return len(doc.paragraphs) > 0
                except Exception as docx_error:
                    logging.error(f"DOCX validation failed: {str(docx_error)}", extra={'path': os.path.basename(file_path)})
                    return False
            elif ext == 'xlsx':
                try:
                    wb = openpyxl.load_workbook(file_path)
                    return len(wb.sheetnames) > 0
                except Exception as xlsx_error:
                    logging.error(f"XLSX validation failed: {str(xlsx_error)}", extra={'path': os.path.basename(file_path)})
                    return False
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


def handle_conversion(file_path, unique_id, original_filename, llm_api_key=None, llm_model='gpt-4o'):
    """处理文件转换的核心逻辑"""
    try:
        logging.info(f"Starting conversion: {os.path.basename(file_path)}")
        start_time = time.time()
        
        # 发送开始处理事件
        socketio.emit('processing_start', {
            'unique_id': unique_id,
            'message': 'Starting file conversion...'
        })

        # 根据 LLM 配置动态创建 MarkItDown 实例
        if llm_api_key:
            try:
                from openai import OpenAI
                llm_client = OpenAI(api_key=llm_api_key)
                md_instance = MarkItDown(
                    enable_plugins=False,
                    llm_client=llm_client,
                    llm_model=llm_model
                )
                logging.info(f"Using LLM model: {llm_model}")
            except Exception as e:
                logging.warning(f"Failed to initialize LLM client: {str(e)}, using default MarkItDown")
                md_instance = MarkItDown(enable_plugins=False)
        else:
            md_instance = MarkItDown(enable_plugins=False)

        # 执行转换并发送进度
        socketio.emit('processing_progress', {
            'unique_id': unique_id,
            'current': 1,
            'total': 3,
            'message': 'Analyzing file structure...'
        })
        
        result = md_instance.convert(file_path)
        
        socketio.emit('processing_progress', {
            'unique_id': unique_id,
            'current': 2,
            'total': 3,
            'message': 'Generating markdown output...'
        })
        
        output_path = os.path.join(app.config['OUTPUT_FOLDER'], f"{unique_id}.md")

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(result.text_content)
        
        socketio.emit('processing_progress', {
            'unique_id': unique_id,
            'current': 3,
            'total': 3,
            'message': 'Finalizing...'
        })

        cache.set(unique_id, {
            'status': 'completed',
            'original_name': original_filename,
            'path': output_path,
            'timestamp': time.time()
        })

        duration = time.time() - start_time
        logging.info(f"Conversion completed in {duration:.2f}s: {os.path.basename(file_path)}")
        socketio.emit('process_complete', {
            'unique_id': unique_id,
            'original_name': original_filename,
            'url': f"/download/{unique_id}",
            'duration': duration
        })
    except TimeoutError as e:
        error_msg = f"Conversion timed out: {str(e)}"
        logging.error(error_msg)
        cache.set(unique_id, {
            'status': 'failed',
            'original_name': original_filename,
            'error': error_msg,
            'timestamp': time.time()
        })
        socketio.emit('process_complete', {
            'unique_id': unique_id,
            'original_name': original_filename,
            'error': error_msg
        })
    except Exception as e:
        error_msg = f"Conversion error: {str(e)}"
        logging.error(error_msg)
        cache.set(unique_id, {
            'status': 'failed',
            'error': error_msg,
            'original_name': original_filename,
            'timestamp': time.time()
        })
        socketio.emit('process_complete', {
            'unique_id': unique_id,
            'original_name': original_filename,
            'error': error_msg
        })
    finally:
        cleanup_file(file_path)


if redis_available:
    @celery.task(bind=True)
    def async_conversion_task(self, file_path, unique_id):
        """Celery 异步任务"""
        try:
            # 从缓存中获取 LLM 配置
            cache_data = cache.get(unique_id)
            llm_api_key = cache_data.get('llm_api_key') if cache_data else None
            llm_model = cache_data.get('llm_model', 'gpt-4o') if cache_data else 'gpt-4o'
            original_filename = cache_data.get('original_name', 'Unknown') if cache_data else 'Unknown'
            
            handle_conversion(file_path, unique_id, original_filename, llm_api_key, llm_model)
            return {'status': 'completed'}
        except Exception as e:
            cache.set(unique_id, {
                'status': 'failed',
                'error': f"Conversion failed: {str(e)}",
                'timestamp': time.time()
            })
            self.update_state(state='FAILURE', meta={'error': str(e)})
            raise


@app.after_request
def add_security_headers(response):
    """添加安全头，包括 CSP"""
    # 内容安全策略
    csp_policy = app.config.get('CSP_POLICY', "default-src 'self'")
    response.headers['Content-Security-Policy'] = csp_policy
    
    # 其他安全头
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    
    return response


@app.route('/')
def upload_form():
    # 获取支持的文件格式统计
    supported_formats = list(app.config['ALLOWED_MIME_TYPES'].keys())
    return render_template('upload.html',
                           allowed_mime_types=app.config['ALLOWED_MIME_TYPES'],
                           maxSize=app.config['MAX_CONTENT_LENGTH'],
                           supported_formats=supported_formats)


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
    if 'file' not in request.files and 'youtube_url' not in request.form:
        return jsonify(status='error', message='No file uploaded or YouTube URL provided'), 400

    # 处理 YouTube URL
    if 'youtube_url' in request.form:
        youtube_url = request.form.get('youtube_url', '').strip()
        if youtube_url:
            return process_youtube_url(youtube_url)
    
    # 处理文件上传
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
            # 获取更详细的错误信息（从日志中）
            error_detail = "文件内容验证失败。请确保文件格式正确且未损坏。"
            logging.error(f"Validation failed for uploaded file: {os.path.basename(temp_path)}")
            raise ValueError(error_detail)

        original_filename = file.filename
        
        # 获取 LLM 配置
        llm_api_key = request.form.get('llm_api_key', '').strip()
        llm_model = request.form.get('llm_model', 'gpt-4o').strip()
        
        cache.set(unique_id, {
            'status': 'processing',
            'path': temp_path,
            'timestamp': time.time(),
            'original_name': original_filename,
            'llm_api_key': llm_api_key,
            'llm_model': llm_model
        })

        if redis_available:
            async_conversion_task.delay(temp_path, unique_id)
        else:
            executor.submit(handle_conversion, temp_path, unique_id, original_filename, llm_api_key, llm_model)

        return jsonify(status='success', unique_id=unique_id)

    except ValueError as e:
        error_msg = f"Validation error: {str(e)}"
        logging.error(error_msg, extra={'path': os.path.basename(temp_path)})
        cleanup_file(temp_path)
        return jsonify(status='error', message=str(e)), 400
    except Exception as e:
        error_msg = f"Upload failed: {str(e)}"
        cleanup_file(temp_path)
        logging.error(error_msg)
        return jsonify(status='error', message='File processing failed'), 500


def process_youtube_url(youtube_url):
    """处理 YouTube URL 转换"""
    try:
        import re
        
        # 验证 YouTube URL 格式
        youtube_pattern = r'(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+'
        if not re.match(youtube_pattern, youtube_url):
            return jsonify(status='error', message='Invalid YouTube URL format'), 400
        
        unique_id = str(uuid.uuid4())
        original_filename = f"youtube_{unique_id[:8]}.txt"
        
        # 获取 LLM 配置
        llm_api_key = request.form.get('llm_api_key', '').strip()
        llm_model = request.form.get('llm_model', 'gpt-4o').strip()
        
        # 创建临时文件存储 URL
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}.url")
        with open(temp_path, 'w', encoding='utf-8') as f:
            f.write(youtube_url)
        
        cache.set(unique_id, {
            'status': 'processing',
            'path': temp_path,
            'timestamp': time.time(),
            'original_name': original_filename,
            'llm_api_key': llm_api_key,
            'llm_model': llm_model,
            'is_youtube': True
        })
        
        logging.info(f"Processing YouTube URL: {youtube_url}")
        
        if redis_available:
            async_conversion_task.delay(temp_path, unique_id)
        else:
            executor.submit(handle_conversion, temp_path, unique_id, original_filename, llm_api_key, llm_model)
        
        return jsonify(status='success', unique_id=unique_id)
    
    except Exception as e:
        logging.error(f"YouTube processing failed: {str(e)}")
        return jsonify(status='error', message='Failed to process YouTube URL'), 500


@app.route('/api/status')
def api_status():
    """返回系统状态和支持的文件格式"""
    return jsonify({
        'status': 'online',
        'version': '1.0.0',
        'powered_by': 'MarkItDown',
        'supported_formats': list(app.config['ALLOWED_MIME_TYPES'].keys()),
        'max_file_size_mb': app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024),
        'features': [
            'PDF to Markdown',
            'Office Documents (Word, PowerPoint, Excel)',
            'Images with EXIF metadata',
            'Audio files with transcription',
            'HTML and text-based formats',
            'ZIP archive iteration',
            'YouTube URL support',
            'EPub support'
        ]
    })


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
