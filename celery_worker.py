"""
Celery Worker 启动文件
用于异步处理文件转换任务

使用方法:
    celery -A celery_worker worker --loglevel=info --pool=solo
    
Windows 环境建议使用 solo 池模式
"""
import os
import sys

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, celery, redis_available

if __name__ == '__main__':
    if not redis_available:
        print("警告：Redis 不可用，Celery Worker 无法启动")
        print("请确保 Redis 服务正在运行")
        sys.exit(1)
    
    # 启动 Celery Worker
    celery.worker_main([
        'worker',
        '--loglevel=info',
        '--pool=solo',  # Windows 兼容模式
        '-Q', 'celery'  # 指定队列
    ])
