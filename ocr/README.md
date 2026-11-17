# ocr

组件·古籍OCR流程模拟。`POST /recognize`仅接受明确标记的文本模拟输入并生成待复核分段；图片请求501。Compose使用共享应用镜像启动该模块，不安装PaddleOCR，不宣称已完成真实古籍图像识别。具体模拟字段见OpenAPI。
