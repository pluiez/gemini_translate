# Gemini Translate 前后端分离版本用户流程时序图

下面的时序图展示了Gemini Translate前后端分离版本中，用户从打开网页到下载翻译结果的完整交互流程。

```mermaid
sequenceDiagram
    participant User as 用户
    participant Frontend as 前端 (React)
    participant Backend as 后端 API (FastAPI)
    participant TaskQueue as 任务队列 (Celery)
    participant ModelAPI as AI 模型 API
    
    User->>Frontend: 1. 访问应用网站
    Frontend-->>User: 加载应用界面
    
    User->>Frontend: 2. 上传CSV文件
    Frontend->>Backend: POST /api/files (文件数据)
    Backend-->>Frontend: 返回文件ID和基本信息
    Frontend->>Backend: GET /api/files/{fileId}/preview
    Backend-->>Frontend: 返回文件预览数据
    Frontend-->>User: 显示文件预览和解析结果
    
    User->>Frontend: 3. 上传API配置文件
    Frontend->>Backend: POST /api/api-configs/upload
    Backend-->>Frontend: API配置文件处理完成
    Frontend-->>User: 显示可用的API配置选项
    
    User->>Frontend: 4. 选择目标语言
    Frontend-->>User: 更新UI状态
    
    User->>Frontend: 5. 选择AI模型
    Frontend-->>User: 更新UI状态
    
    User->>Frontend: 6. 配置API参数
    Frontend->>Backend: POST /api/api-configs/settings
    Backend-->>Frontend: 配置保存确认
    Frontend-->>User: 显示配置已保存
    
    User->>Frontend: 7. 开始翻译
    Frontend->>Backend: POST /api/tasks (创建翻译任务)
    Backend->>TaskQueue: 创建异步翻译任务
    Backend-->>Frontend: 返回任务ID
    Frontend-->>User: 显示任务已创建
    
    Frontend->>Backend: GET /api/tasks/{taskId} (轮询状态)
    Backend->>TaskQueue: 查询任务状态
    TaskQueue-->>Backend: 返回任务进度 (0%)
    Backend-->>Frontend: 任务进度更新
    Frontend-->>User: 显示初始进度 (0%)
    
    TaskQueue->>ModelAPI: 批量发送翻译请求
    Note right of ModelAPI: 并行处理多个<br/>翻译项
    
    Frontend->>Backend: GET /api/tasks/{taskId} (轮询状态)
    Backend->>TaskQueue: 查询任务状态
    TaskQueue-->>Backend: 返回任务进度 (25%)
    Backend-->>Frontend: 任务进度更新
    Frontend-->>User: 显示进度 (25%)
    
    Frontend->>Backend: GET /api/tasks/{taskId} (轮询状态)
    Backend->>TaskQueue: 查询任务状态
    TaskQueue-->>Backend: 返回任务进度 (50%)
    Backend-->>Frontend: 任务进度更新
    Frontend-->>User: 显示进度 (50%)
    
    Frontend->>Backend: GET /api/tasks/{taskId} (轮询状态)
    Backend->>TaskQueue: 查询任务状态
    TaskQueue-->>Backend: 返回任务进度 (75%)
    Backend-->>Frontend: 任务进度更新
    Frontend-->>User: 显示进度 (75%)
    
    ModelAPI-->>TaskQueue: 返回全部翻译结果
    TaskQueue->>Backend: 更新任务状态为完成
    
    Frontend->>Backend: GET /api/tasks/{taskId} (轮询状态)
    Backend-->>Frontend: 任务完成状态 (100%)
    Frontend->>Backend: GET /api/tasks/{taskId}/results
    Backend-->>Frontend: 返回翻译结果数据
    Frontend-->>User: 显示翻译完成和结果预览
    
    User->>Frontend: 8. 下载结果
    Frontend->>Backend: GET /api/tasks/{taskId}/download
    Backend-->>Frontend: 返回压缩文件流 (.zip)
    Frontend-->>User: 浏览器下载压缩文件
```

## 流程说明

1. **访问应用**: 用户打开前端应用，加载React SPA界面
2. **上传数据**: 用户上传CSV文件，前端发送到后端API进行处理和预览
3. **上传API配置**: 用户上传API配置文件，系统加载可用的API选项
4. **选择翻译设置**: 用户选择目标语言、AI模型并配置API参数
5. **任务创建**: 前端通过API创建翻译任务，后端将任务加入队列
6. **状态轮询**: 前端定期轮询后端获取任务进度
7. **异步处理**: 后端任务队列管理与AI模型API的通信，并行处理翻译请求
8. **结果获取**: 翻译完成后，前端获取结果并展示给用户
9. **下载导出**: 用户下载结果，系统将翻译后的文件打包为压缩文件提供下载

此时序图反映了前后端分离架构的特点，包括明确的API调用、状态轮询机制以及任务队列的使用，这与原有的Gradio版本在交互模式上有显著区别。 