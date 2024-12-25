# File2MD (markitdown Web)

## 项目简介

这是一个基于 [markitdown](https://github.com/microsoft/markitdown) 与 Flask 的 Web 应用程序，允许用户上传各种类型的文件并将其转换为 Markdown 格式。

## 功能

- 支持上传多种文件格式，包括 PDF、Word、Excel、图片等。
- 使用 MarkItDown 库将上传的文件转换为 Markdown 格式。
- 实时反馈文件处理状态。
- 通过 Redis 和 Celery 实现异步文件处理。
- 定期清理未访问的输出文件，以节省存储空间。

## AI工具推荐
邮箱验证码登录，注册无忧！
轻松登录，无需繁琐注册，立即获得 200 额度礼包，开启你的 AI 探索之旅！

Anakin —— 使用 AI 提升所有人的工作效率 现在注册，立即获得 200 额度礼包，使用数量多达 200+ 个 AI 应用（文本生成、图像绘制）！你也可以轻松地搭建出自己的专属 AI 应用，快来体验吧
🔗 注册链接：https://anakin.ai/?r=7trees

## 安装和运行

1. **克隆本仓库**：

   ```bash
   git clone https://github.com/Athenavi/File2MD
   cd File2MD

创建虚拟环境并激活（可选）：
```
python -m venv venv
source venv/bin/activate  

# On Windows use `venv\Scripts\activate`
```

安装依赖：

`pip install -r requirements.txt`

配置环境变量(可选)：

创建 .env 文件并添加以下内容：
```
REDIS_HOST=localhost
REDIS_PORT=6379
UPLOAD_FOLDER=uploads/
OUTPUT_FOLDER=output/
CELERY_BROKER_URL=redis://localhost:6379/0
```

启动应用：

`python app.py`
然后在浏览器中访问 http://127.0.0.1:5000