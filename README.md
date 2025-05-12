# File2MD（MarkItDown Web Converter） 

🔄📄➡️📝

[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.0%2B-lightgrey.svg)](https://flask.palletsprojects.com)

现代文件转换服务，支持多种文档格式转Markdown，提供安全高效的处理能力。

## 🌟 核心特性

- **多格式支持**  
  PDF、DOCX、PPTX、XLSX、图片等 ➡️ Markdown转换
- **云端异步处理**  
  Celery + Redis分布式任务队列
- **实时进度推送**  
  WebSocket实时状态更新
- **企业级安全**  
  文件签名验证 + 内容安全策略（CSP）
- **智能资源管理**  
  自动清理过期文件 + 内存保护机制
- **生产就绪**  
  限流控制 + 监控日志 + 异常处理

## 🛠 技术栈

**核心框架**  
![Flask](https://img.shields.io/badge/-Flask-000000?logo=flask) 
![Celery](https://img.shields.io/badge/-Celery-37814A?logo=celery)

**数据处理**  
![PyPDF2](https://img.shields.io/badge/-PyPDF2-0066CC) 
![Pillow](https://img.shields.io/badge/-Pillow-3776AB?logo=python)

**基础设施**  
![Redis](https://img.shields.io/badge/-Redis-DC382D?logo=redis) 
![Docker](https://img.shields.io/badge/-Docker-2496ED?logo=docker)

**安全防护**  
![CSP](https://img.shields.io/badge/-CSP-FF6B6B) 
![Rate_Limiting](https://img.shields.io/badge/-Rate%20Limiting-4ECDC4)

## 🚀 快速开始

### 环境要求

- Python 3.12+
- Redis Server
- libmagic (文件类型检测)

### 安装步骤

```bash
# 克隆仓库
git clone https://github.com/Athenavi/File2MD
cd File2MD

# 安装依赖
pip install -r requirements.txt
```

### 配置说明 (`.env`)

```ini
# 存储配置
UPLOAD_FOLDER=./uploads
OUTPUT_FOLDER=./outputs

# Redis配置
REDIS_HOST=localhost
REDIS_PORT=6379

# 安全配置
FILE_RETENTION_HOURS=2
MAX_CONTENT_LENGTH=52428800  # 50MB
```

### 启动服务

```bash
# 开发模式
python app.py

# 生产模式 (需要安装gunicorn)
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

## 📡 API 文档

### 文件上传

```http
POST /upload
Content-Type: multipart/form-data

Response:
{
  "status": "success",
  "unique_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

### 下载转换结果

```http
GET /download/{uuid}
Response: Markdown文件下载
```

### 实时状态查询

通过WebSocket连接获取实时处理状态：

```javascript
socket.on('process_complete', (data) => {
  console.log('Conversion completed:', data);
});
```

## 🔒 安全特性

- **深度文件验证**  
  双重校验（文件头+内容结构分析）
- **请求限流**  
  50请求/小时/IP 的默认策略
- **日志脱敏**  
  自动过滤敏感路径信息
- **沙箱处理**  
  独立线程池执行转换任务
- **CSP防护**  
  默认启用严格内容安全策略

## 🤝 贡献指南

欢迎通过Issue或PR参与贡献：

1. Fork项目仓库
2. 创建特性分支 (`git checkout -b feature/your-feature`)
3. 提交修改 (`git commit -am 'Add some feature'`)
4. 推送分支 (`git push origin feature/your-feature`)
5. 创建Pull Request
