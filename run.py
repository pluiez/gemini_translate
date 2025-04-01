import json
import argparse
import tempfile
import time
import zipfile
import os
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from collections import defaultdict

import gradio as gr
import pandas as pd
from openai import OpenAI

import generate_requests

# 设置日志记录器
logger = logging.getLogger("gemini_translate")
handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)


import re


def parse_first_valid_json(text: str):
    """
    在给定的文本中，搜寻所有形如 ```...``` 或 ```json ...``` 的代码块，
    并返回第一个可成功使用 json.loads() 解析的代码块所对应的 Python 对象。
    如果找不到任何有效的 JSON，就抛出 ValueError。
    """
    # 1. 匹配所有三引号包裹的代码块。r"```(\w+)?([\s\S]*?)```"：
    #    - (\w+)? 匹配可选的语言标识，比如 "json"
    #    - ([\s\S]*?) 匹配代码块内容（非贪婪）
    code_blocks = re.findall(r"```(\w+)?([\s\S]*?)```", text)

    # 2. 依次尝试对代码块进行 JSON 解析
    for lang, block_content in code_blocks:
        if lang.lower() != "json" and lang != "":
            continue

        d = load_dirty_json(block_content)
        return d

    message = f"No valid JSON found in text: {text}"
    raise ValueError(message)


def fix_broken_json(json_str: str) -> str:
    """
    对包含未转义双引号的 JSON 文本进行修正，返回能被 json.loads() 解析的合法 JSON。
    该函数主要针对如下类似情形进行修复：
        "Title": "...50"x 60"..."
    将内部出现的 " 替换为 \"。
    使用方式：
        valid_json_str = fix_broken_json(original_json_str)
        data = json.loads(valid_json_str)
    """
    # 思路：
    # 1. 先整体去掉首尾空白后，保留第一行 '{' 和最后一行 '}'，中间行逐行处理；
    # 2. 对于形如  "Key": "Value", 的行，用正则捕获 Key 和 Value；
    # 3. 对 value 中的引号进行转义处理，重新拼装该行；
    # 4. 合并各行并返回修正后的字符串。

    lines = json_str.strip().split('\n')

    # 若行数太少，或者非典型 JSON 格式，直接尝试原样返回
    if len(lines) < 2:
        return json_str

    new_lines = []
    # 首行保持为 {，末行保持为 }
    new_lines.append('{')

    for i in range(1, len(lines) - 1):
        line_stripped = lines[i].strip()

        # 判断该行是否以逗号结尾，记录下来，便于后续拼装
        has_comma = False
        if line_stripped.endswith(','):
            has_comma = True
            line_stripped = line_stripped[:-1].rstrip()

        # 使用正则匹配 "Key": "Value"
        # 注意这里的贪婪/惰性匹配，Value 里可以含有其他字符；我们只要捕获外层引号之间的内容
        pattern = r'^"([^"]+)"\s*:\s*"([\s\S]*)"$'
        match_obj = re.match(pattern, line_stripped)

        if not match_obj:
            # 如果不符合行的典型格式，则原样拼回
            # 这意味着可能是空白行或者别的结构（比如末尾没有逗号或某些特殊行）
            # 看情况也可以选择直接忽略
            # 这里选择拼回，以免丢失信息
            if has_comma:
                new_lines.append(line_stripped + ",")
            else:
                new_lines.append(line_stripped)
            continue

        key = match_obj.group(1)
        val = match_obj.group(2)

        # 对值中的引号做转义处理
        # 先把反斜杠本身转义一次，避免后面重复转义
        val = val.replace('\\', '\\\\')
        # 再把未转义的引号（双引号）转义
        val = val.replace('"', '\\"')

        new_line = f"\"{key}\": \"{val}\""
        if has_comma:
            new_line += ","
        new_lines.append(new_line)

    new_lines.append('}')

    return '\n'.join(new_lines)

def load_dirty_json(json_str):
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        return json.loads(fix_broken_json(json_str))




def generate_requests_from_file(filepath, target_language):
    inputs = generate_requests.load_csv_as_dicts(filepath)
    filtered = generate_requests.filter_keys(inputs)

    requests = []
    for request in generate_requests.generate_requests(filtered, target_language):
        request["filepath"] = filepath
        requests.append(request)

    return [inputs, requests]


def upload(filepaths, state, progress=gr.Progress()):
    for f in progress.tqdm(filepaths):
        ...

    # update state
    state["files"] = filepaths

    return [
        gr.UploadButton(interactive=False),
        gr.Button("Start", interactive=True),
        state,
    ]


def remove_markdown_code_syntax(text):
    text = text.strip()
    left = text.find("```")
    assert left != -1, f"Expected markdown code block, got {text}"
    left = text.find("\n", left)
    right = text.rfind("```", left + 1)
    assert right != -1, f"Expected markdown code block, got {text}"
    return text[left + 1 : right]


# 计算字典内容的hash值
def calculate_hash(data):
    """计算字典内容的哈希值，用于检测重复内容"""
    # 将字典转换为排序后的JSON字符串，确保相同内容产生相同哈希值
    json_str = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(json_str.encode("utf-8")).hexdigest()


# 检查是否已经处理过相同内容, 返回 batch-outputs-{hash_value}.jsonl 和 {hash_value}.zip
def check_processed_file(hash_value):
    """检查是否存在指定哈希值的处理结果文件"""
    batch_output_file = f"batch_data/batch-outputs.{hash_value}.jsonl"
    zip_file = f"outputs/{hash_value}.zip"
    batch_output_file = batch_output_file if os.path.exists(batch_output_file) else None
    zip_file = zip_file if os.path.exists(zip_file) else None
    return batch_output_file, zip_file


# 保存filepath2inputs到本地文件
def save_filepath2inputs(filepath2inputs, requests_hash):
    os.makedirs("batch_data", exist_ok=True)
    filepath = f"batch_data/filepath2inputs.{requests_hash}.jsonl"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(filepath2inputs, f, ensure_ascii=False)
    return filepath


# 创建batch输入文件
def create_batch_input_file(requests, model, requests_hash):
    os.makedirs("batch_data", exist_ok=True)
    filepath = f"batch_data/batch_inputs.{requests_hash}.jsonl"

    with open(filepath, "w", encoding="utf-8") as f:
        for i, request in enumerate(requests):
            # 直接使用原始UUID作为custom_id
            original_uuid = request["uuid"]

            batch_request = {
                "custom_id": original_uuid,
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {"model": model, "messages": request["messages"]},
            }
            f.write(json.dumps(batch_request, ensure_ascii=False) + "\n")

    # 不再需要保存UUID映射关系
    return filepath, None


# 添加新函数来记录和获取任务文件映射关系
def save_batch_file_mapping(batch_id, file_info):
    """保存批次ID到文件映射的信息"""
    mapping_file = os.path.join("batch_data", "batch_file_mapping.json")

    # 读取现有映射
    if os.path.exists(mapping_file):
        with open(mapping_file, "r", encoding="utf-8") as f:
            try:
                mappings = json.load(f)
            except json.JSONDecodeError:
                mappings = {}
    else:
        mappings = {}

    # 更新映射
    mappings[batch_id] = file_info

    # 保存映射
    with open(mapping_file, "w", encoding="utf-8") as f:
        json.dump(mappings, f, ensure_ascii=False, indent=2)

    print(f"已保存批次ID {batch_id} 的文件映射信息")
    return mappings


def save_requests_hash_to_batch_id(requests_hash, batch_id):
    mapping_file = os.path.join(
        "batch_data", f"requests_hash_to_batch_id.{requests_hash}.txt"
    )
    with open(mapping_file, "w", encoding="utf-8") as w:
        w.write(f"{batch_id}\n")


def save_batch_id_to_requests_hash(batch_id, requests_hash):
    mapping_file = os.path.join(
        "batch_data", f"batch_id_to_requests_hash.{batch_id}.txt"
    )
    with open(mapping_file, "w", encoding="utf-8") as w:
        w.write(f"{requests_hash}\n")


def get_batch_id_from_requests_hash(requests_hash):
    mapping_file = os.path.join(
        "batch_data", f"requests_hash_to_batch_id.{requests_hash}.txt"
    )
    assert os.path.exists(mapping_file), f"找不到请求哈希值 {requests_hash} 的批次ID映射文件"
    with open(mapping_file, "r", encoding="utf-8") as f:
        return f.read().strip()


def get_requests_hash_from_batch_id(batch_id):
    mapping_file = os.path.join(
        "batch_data", f"batch_id_to_requests_hash.{batch_id}.txt"
    )
    assert os.path.exists(mapping_file), f"找不到批次ID {batch_id} 的请求哈希值映射文件"
    with open(mapping_file, "r", encoding="utf-8") as f:
        return f.read().strip()


# 修改submit_batch_task函数，保存文件映射信息
def submit_batch_task(input_file_path, _, api_config):
    """提交批处理任务并返回任务信息"""
    client = OpenAI(api_key=api_config["api_key"], base_url=api_config["base_url"])

    # 上传文件
    with open(input_file_path, "rb") as f:
        file_object = client.files.create(file=f, purpose="batch")

    # 创建batch任务
    batch = client.batches.create(
        input_file_id=file_object.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
    )

    # 创建任务信息
    task = {
        "batch_id": batch.id,
        "status": batch.status,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "completed_at": None,
        "failed_at": None,
        "expired_at": None,
        "cancelled_at": None,
        "input_file_id": file_object.id,
        "output_file_id": None,
        "error_file_id": None,
        "error_message": None,
    }

    return task


# 检查batch任务状态
def check_batch_status(batch_id, api_config):
    client = OpenAI(api_key=api_config["api_key"], base_url=api_config["base_url"])

    batch = client.batches.retrieve(batch_id=batch_id)

    status_update = {
        "status": batch.status,
        "output_file_id": batch.output_file_id,
        "error_file_id": batch.error_file_id,
    }

    # 提取错误信息
    error_message = None
    if (
        hasattr(batch, "errors")
        and batch.errors
        and hasattr(batch.errors, "data")
        and batch.errors.data
    ):
        error_messages = []
        for error in batch.errors.data:
            if hasattr(error, "message"):
                error_messages.append(f"{error.code}: {error.message}")

        if error_messages:
            error_message = "; ".join(error_messages)

    status_update["error_message"] = error_message

    # 更新完成/失败/过期/取消时间
    if batch.status == "completed" and not status_update.get("completed_at"):
        status_update["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elif batch.status == "failed" and not status_update.get("failed_at"):
        status_update["failed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elif batch.status == "expired" and not status_update.get("expired_at"):
        status_update["expired_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elif batch.status == "cancelled" and not status_update.get("cancelled_at"):
        status_update["cancelled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return status_update


# 取消batch任务
def cancel_batch_task(batch_id, api_config):
    client = OpenAI(api_key=api_config["api_key"], base_url=api_config["base_url"])

    batch = client.batches.cancel(batch_id=batch_id)

    return {
        "status": batch.status,
        "cancelled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# 下载batch任务结果并处理
def process_batch_results(requests_hash, api_config):
    """处理批处理任务结果，返回结果文件路径"""
    batch_id = get_batch_id_from_requests_hash(requests_hash)
    logger.info(f"开始处理批处理任务结果: batch_id={batch_id}, requests_hash={requests_hash}")

    filepath2inputs_file = f"batch_data/filepath2inputs.{requests_hash}.jsonl"
    requests_file = f"batch_data/requests.{requests_hash}.jsonl"

    with open(filepath2inputs_file, "r", encoding="utf-8") as f:
        filepath2inputs = json.load(f)

    with open(requests_file, "r", encoding="utf-8") as f:
        requests = json.load(f)

    uuid2requests = {}

    for request in requests:
        uuid2requests[request["uuid"]] = request

    batch_output_file = f"batch_data/batch_outputs.{requests_hash}.jsonl"
    if not os.path.exists(batch_output_file):
        logger.info(f"批处理输出文件 {batch_output_file} 不存在，尝试从API下载，batch_id: {batch_id}")
        client = OpenAI(
            api_key=api_config.get("api_key"), base_url=api_config.get("base_url"),
        )
        batch_task = client.batches.retrieve(batch_id)
        output_file_id = batch_task.output_file_id
        content = client.files.content(output_file_id)
        content.write_to_file(batch_output_file)
    else:
        logger.info(f"批处理输出文件 {batch_output_file} 已存在，跳过下载")

    logger.info("正在解析下载的批处理结果内容...")
    raw_results = parse_batch_results(batch_output_file)

    # 创建临时目录用于生成CSV文件
    with tempfile.TemporaryDirectory() as temp_dir_path:
        filepath2uuid2inputs = {}

        # 将filepath2inputs转换为按UUID索引的结构
        for filepath, inputs_list in filepath2inputs.items():
            if filepath not in filepath2uuid2inputs:
                filepath2uuid2inputs[filepath] = {}
            for input_item in inputs_list:
                filepath2uuid2inputs[filepath][input_item["uuid"]] = input_item

        for filepath, uuid2inputs in filepath2uuid2inputs.items():
            logger.info(f"文件 {filepath} 包含 {len(uuid2inputs)} 个输入项")

        count_failed = 0
        # 处理每个翻译结果
        for request_uuid, result in raw_results.items():
            # 从请求UUID获取原始数据的UUID（请求UUID格式为 "{原始UUID}.{index}"）
            original_uuid = request_uuid.split('.')[0]
            response_text = result["text"]

            request = uuid2requests[request_uuid]

            request_type = request["type"]

            # 查找对应的原始输入项
            for filepath, uuid2inputs in filepath2uuid2inputs.items():
                if original_uuid not in uuid2inputs:
                    continue

                input_item = uuid2inputs[original_uuid]

                # 根据请求类型处理翻译结果
                try:
                    if request_type == "html":
                        # HTML类型的处理
                        key_name = request["key"]
                        input_item["data"][key_name] = remove_markdown_code_syntax(response_text)
                    elif request_type == "json":
                        # JSON类型的处理
                        try:
                            translation_dict = parse_first_valid_json(response_text)
                        except json.JSONDecodeError as e:
                            raise ValueError(f"Invalid JSON: {response_text}") from e
                        except Exception as e:
                            raise ValueError(f"Invalid JSON: {response_text}") from e
                        input_item["data"].update(translation_dict)
                    else:
                        raise NotImplementedError(f"Unknown request type: {request_type}")
                except Exception as e:
                    count_failed += 1
                    logger.error(f"处理失败: {e}")
        gr.Warning(f"处理失败: {count_failed} 个请求")
        logger.warning(f"处理失败: {count_failed} 个请求")

        for filepath, inputs in filepath2inputs.items():
            # write to csv
            output_path = f"{temp_dir_path}/{Path(filepath).name}"
            # convert inputs to pandas dataframe, inputs is a list of dict
            df = pd.DataFrame([x["data"] for x in inputs])
            df.to_csv(output_path, index=False)

            # write to jsonl
            output_path = f"{temp_dir_path}/{Path(filepath).name}.jsonl"
            with open(output_path, "w") as w:
                for x in inputs:
                    w.write(json.dumps(x["data"]) + "\n")

        zip_file_path = f"outputs/{requests_hash}.zip"

        # Create a zip file containing all files from the temporary directory
        with zipfile.ZipFile(zip_file_path, "w") as zipf:
            for file_path in Path(temp_dir_path).iterdir():
                if file_path.is_file():
                    zipf.write(file_path, file_path.relative_to(temp_dir_path))

        logger.info(f"Created zip archive: {zip_file_path}")

    return zip_file_path


def do_submit_batch_task(
    input_state,
    model,
    target_language,
    api_config,
    batch_tasks_state,
    progress=gr.Progress(),
):
    logger.info(f"model: {model}")
    logger.info(f"api_config: {api_config}")
    logger.info(f"target_language: {target_language}")

    requests = []
    filepath2inputs = {}

    gr.Info("Generating requests...", title="Processing", duration=3)

    for filepath in progress.tqdm(
        input_state["files"], desc="Generating requests", unit="file"
    ):
        file_inputs, file_requests = generate_requests_from_file(
            filepath, target_language
        )
        filepath2inputs[filepath] = file_inputs
        requests += file_requests

    # 计算请求的哈希值
    requests_hash = calculate_hash(requests)
    logger.info(f"原始请求哈希值: {requests_hash}")

    # 保存filepath2inputs和requests信息
    save_filepath2inputs(filepath2inputs, requests_hash)
    save_requests(requests, requests_hash)

    # 创建batch输入文件
    # f"batch_data/batch_inputs.{requests_hash}.jsonl"
    batch_input_file, _ = create_batch_input_file(
        requests, model, requests_hash
    )

    # 提交batch任务
    gr.Info("Submitting batch task...", title="Processing", duration=3)
    batch_task = submit_batch_task(batch_input_file, None, api_config)
    # f"batch_data/requests_hash_to_batch_id.{requests_hash}.txt"
    # f"batch_data/batch_id_to_requests_hash.{batch_task['batch_id']}.txt"
    save_requests_hash_to_batch_id(requests_hash, batch_task["batch_id"])
    save_batch_id_to_requests_hash(batch_task["batch_id"], requests_hash)

    # 更新batch任务状态
    batch_tasks = batch_tasks_state["tasks"]
    # 添加到列表开头，确保最新任务在最前面
    batch_tasks.insert(0, batch_task)
    batch_tasks_state["tasks"] = batch_tasks

    # 准备表格数据
    table_data = []
    for task in batch_tasks:
        table_data.append(
            [
                task["batch_id"],
                task["status"],
                task["created_at"],
                task["completed_at"] or "",
                task["failed_at"] or "",
                task["expired_at"] or "",
                task["cancelled_at"] or "",
                task["error_message"] or "",
            ]
        )

    return [
        f"Batch task submitted. Batch ID: {batch_task['batch_id']}，Hash: {requests_hash}",
        batch_tasks_state,
        gr.DataFrame(
            value=table_data,
            headers=[
                "Batch ID",
                "Status",
                "Created At",
                "Completed At",
                "Failed At",
                "Expired At",
                "Cancelled At",
                "Error Message",
            ],
        ),
    ]


def on_input_state_changed(state):
    filepaths = state["files"]
    text = f"Files：{len(filepaths)}"
    text = text + "\n\n" + "\n".join(Path(x).name for x in filepaths)
    return text


# 获取批处理任务列表
def list_batch_tasks(api_config):
    """获取批处理任务列表"""
    if not api_config:
        return []

    client = OpenAI(api_key=api_config["api_key"], base_url=api_config["base_url"])

    try:
        # 查询最新的批处理任务列表，限制最多20个
        batches_result = client.batches.list(limit=10)

        # 将任务信息转换为统一格式
        tasks = []
        for batch in batches_result.data:
            # 提取错误信息
            error_message = None
            if (
                hasattr(batch, "errors")
                and batch.errors
                and hasattr(batch.errors, "data")
                and batch.errors.data
            ):
                error_messages = []
                for error in batch.errors.data:
                    if hasattr(error, "message"):
                        error_messages.append(f"{error.code}: {error.message}")

                if error_messages:
                    error_message = "; ".join(error_messages)

            # 检查是否存在本地批处理数据文件
            batch_id = batch.id
            status = batch.status

            task = {
                "batch_id": batch_id,
                "status": status,
                "created_at": datetime.fromtimestamp(batch.created_at).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if batch.created_at
                else "",
                "completed_at": datetime.fromtimestamp(batch.completed_at).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if batch.completed_at
                else None,
                "failed_at": None,
                "expired_at": None,
                "cancelled_at": None,
                "input_file_id": batch.input_file_id,
                "output_file_id": batch.output_file_id,
                "error_file_id": batch.error_file_id,
                "error_message": error_message,
            }

            tasks.append(task)

        # 按创建时间倒序排序，确保最新任务在最前面
        tasks.sort(
            key=lambda x: x["created_at"] if isinstance(x["created_at"], str) else "",
            reverse=True,
        )

        return tasks
    except Exception as e:
        logger.error(f"获取批处理任务列表失败: {str(e)}")
        return []


def on_upload_api_config(filepath, batch_tasks_state):
    with open(filepath, "r") as r:
        api_config = json.load(r)

    # 限制只能包含一个模型
    if len(api_config) > 1:
        gr.Warning("API配置文件只能包含一个模型。将只使用第一个模型。")
        model_name = list(api_config.keys())[0]
        api_config = {model_name: api_config[model_name]}

    # 确保api_config包含base_url和api_key
    model_name = list(api_config.keys())[0]
    model_config = api_config[model_name]

    if "base_url" not in model_config or "api_key" not in model_config:
        gr.Error("API配置文件必须包含base_url和api_key字段。")
        # 返回空表格
        return [
            {"api_config": {}},
            {},
            gr.Dropdown(choices=[]),
            batch_tasks_state,
            gr.DataFrame(
                value=[],
                headers=[
                    "Batch ID",
                    "Status",
                    "Created At",
                    "Completed At",
                    "Failed At",
                    "Expired At",
                    "Cancelled At",
                    "Error Message",
                ],
            ),
        ]

    # 简化api_config结构
    simplified_config = {
        "model": model_name,
        "base_url": model_config["base_url"],
        "api_key": model_config["api_key"],
    }

    # 获取批处理任务列表
    tasks = list_batch_tasks(simplified_config)

    # 更新批处理任务状态
    new_batch_tasks_state = {"tasks": tasks}

    # 准备表格数据
    table_data = []
    for task in tasks:
        table_data.append(
            [
                task["batch_id"],
                task["status"],
                task["created_at"],
                task["completed_at"] or "",
                task["failed_at"] or "",
                task["expired_at"] or "",
                task["cancelled_at"] or "",
                task["error_message"] or "",
            ]
        )

    return [
        simplified_config,
        gr.Dropdown(choices=[model_name], value=model_name, interactive=True),
        new_batch_tasks_state,
        gr.DataFrame(
            value=table_data,
            headers=[
                "Batch ID",
                "Status",
                "Created At",
                "Completed At",
                "Failed At",
                "Expired At",
                "Cancelled At",
                "Error Message",
            ],
        ),
    ]


def update_batch_tasks(batch_tasks_state, api_config):
    if not api_config or not batch_tasks_state["tasks"]:
        return [
            batch_tasks_state,
            gr.DataFrame(
                value=[],
                headers=[
                    "Batch ID",
                    "Status",
                    "Created At",
                    "Completed At",
                    "Failed At",
                    "Expired At",
                    "Cancelled At",
                    "Error Message",
                ],
            ),
        ]

    tasks = batch_tasks_state["tasks"]
    updated = False

    # 更新所有任务的状态，确保completed任务也会获取output_file_id
    for task in tasks:
        if task["status"] not in (
            "in_progress",
            "validating",
            "finalizing",
            "cancelling",
        ):
            continue

        try:
            status_update = check_batch_status(task["batch_id"], api_config)
            task.update(status_update)
            updated = True
        except Exception as e:
            print(f"Error updating task {task['batch_id']}: {e}")

    if updated:
        batch_tasks_state["tasks"] = tasks

    # 准备表格数据
    table_data = []
    for task in tasks:
        table_data.append(
            [
                task["batch_id"],
                task["status"],
                task["created_at"],
                task["completed_at"] or "",
                task["failed_at"] or "",
                task["expired_at"] or "",
                task["cancelled_at"] or "",
                task["error_message"] or "",
            ]
        )

    return [
        batch_tasks_state,
        gr.DataFrame(
            value=table_data,
            headers=[
                "Batch ID",
                "Status",
                "Created At",
                "Completed At",
                "Failed At",
                "Expired At",
                "Cancelled At",
                "Error Message",
            ],
        ),
    ]


def on_cancel_batch_task(batch_id, batch_tasks_state, api_config):
    if not api_config:
        return [batch_tasks_state, gr.DataFrame()]

    tasks = batch_tasks_state["tasks"]
    for task in tasks:
        if task["batch_id"] == batch_id:
            if task["status"] in ["validating", "in_progress"]:
                try:
                    status_update = cancel_batch_task(batch_id, api_config)
                    task.update(status_update)
                    gr.Info(f"Task {batch_id} cancelled successfully.")
                except Exception as e:
                    gr.Error(f"Failed to cancel task {batch_id}: {e}")
            else:
                gr.Warning(
                    f"Task {batch_id} cannot be cancelled in status: {task['status']}"
                )
            break

    batch_tasks_state["tasks"] = tasks

    # 准备表格数据
    table_data = []
    for task in tasks:
        table_data.append(
            [
                task["batch_id"],
                task["status"],
                task["created_at"],
                task["completed_at"] or "",
                task["failed_at"] or "",
                task["expired_at"] or "",
                task["cancelled_at"] or "",
                task["error_message"] or "",
            ]
        )

    return [
        batch_tasks_state,
        gr.DataFrame(
            value=table_data,
            headers=[
                "Batch ID",
                "Status",
                "Created At",
                "Completed At",
                "Failed At",
                "Expired At",
                "Cancelled At",
                "Error Message",
            ],
        ),
    ]


# 修改on_download_batch_results函数，添加处理结果的逻辑
def on_download_batch_results(batch_id, batch_tasks_state, api_config):
    """下载批处理任务结果"""
    logger.info(f"开始处理下载请求: batch_id={batch_id}")

    # 如果没有选中任务，返回None
    if not batch_id:
        logger.warning("未选择任务，无法下载")
        gr.Warning("请先选择一个任务")
        return None

    tasks = batch_tasks_state["tasks"]
    selected_task = None

    for task in tasks:
        if task["batch_id"] == batch_id:
            selected_task = task
            logger.info(f"从任务列表中找到任务: {batch_id}, 状态: {task['status']}")
            break

    if not selected_task:
        logger.warning(f"找不到任务ID: {batch_id}")
        gr.Warning(f"找不到任务ID: {batch_id}")
        return None

    # 确认任务已完成
    if selected_task["status"] != "completed":
        logger.warning(f"任务 {batch_id} 尚未完成，当前状态: {selected_task['status']}")
        gr.Warning(f"任务尚未完成，无法下载结果。当前状态: {selected_task['status']}")
        return None

    requests_hash = get_requests_hash_from_batch_id(batch_id)

    # 先查找最终处理结果文件
    batch_output_file, zip_file = check_processed_file(requests_hash)

    if zip_file:
        logger.info(f"找到已处理的结果文件: {zip_file}")
        gr.Info("找到已处理的结果文件", title="Processing", duration=3)
        # 直接返回文件路径，适用于DownloadButton
        return zip_file

    gr.Info("正在处理批处理结果，请稍候...", title="Processing", duration=5)

    zip_file = process_batch_results(requests_hash, api_config)

    logger.info(f"成功处理批处理结果，生成结果文件: {zip_file}")
    gr.Info("批处理结果处理完成", title="Processing", duration=3)

    # 直接返回文件路径
    return zip_file


# 修改update_selected_task函数，调整按钮文本
def update_selected_task(evt: gr.SelectData, batch_tasks_state):
    """更新选中的任务ID并设置按钮状态"""
    if not batch_tasks_state or "tasks" not in batch_tasks_state:
        logger.error("批次任务状态无效，无法更新选中任务")
        return [
            "",
            gr.Button(value="取消选中任务", interactive=False),
            gr.update(interactive=False),
        ]

    # 获取选中的行索引
    row_index = evt.index[0] if hasattr(evt, "index") and evt.index else 0

    # 检查索引是否有效
    if row_index >= len(batch_tasks_state["tasks"]):
        logger.error(f"无效的行索引: {row_index}, 任务总数: {len(batch_tasks_state['tasks'])}")
        return [
            "",
            gr.Button(value="取消选中任务", interactive=False),
            gr.update(interactive=False),
        ]

    # 获取选中的任务
    selected_task = batch_tasks_state["tasks"][row_index]
    selected_batch_id = selected_task["batch_id"]

    logger.info(f"选中任务: batch_id={selected_batch_id}, 状态={selected_task['status']}")

    # 根据任务状态设置按钮状态
    can_cancel = selected_task["status"] in ["validating", "in_progress"]
    can_download = selected_task["status"] == "completed"

    # 返回更新的状态
    return [
        selected_batch_id,
        gr.Button(value="取消选中任务", interactive=can_cancel),
        gr.update(interactive=can_download),
    ]


def parse_batch_results(batch_output_file):
    """解析批处理任务的结果内容"""
    results = {}

    # 尝试解析为JSON行格式
    with open(batch_output_file, "r", encoding="utf-8") as r:
        for line in r:
            result_json = json.loads(line)
            custom_id = result_json["custom_id"]  # 直接使用原始的请求UUID

            response = result_json["response"]["body"]["choices"][0]["message"][
                "content"
            ]

            results[custom_id] = {
                "text": response,
                "raw": result_json,
            }

    logger.info(f"批处理结果解析完成: 共 {len(results)} 行")

    return results


# 保存requests到本地文件
def save_requests(requests, requests_hash):
    os.makedirs("batch_data", exist_ok=True)
    # 计算内容哈希值
    filepath = f"batch_data/requests.{requests_hash}.jsonl"

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(requests, f, ensure_ascii=False)

    logger.info(f"保存了 {len(requests)} 个请求信息到文件: {filepath}")
    return filepath

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--share", choices=[0, 1], required=True, type=int)
    args = parser.parse_args()

    args.share = bool(args.share)
    return args

if __name__ == "__main__":
    args = parse_args()

    print(f"args: {args}")

    # 创建输出目录
    os.makedirs("outputs", exist_ok=True)
    os.makedirs("batch_data", exist_ok=True)

    with gr.Blocks() as demo:
        api_config_state = gr.State({})
        input_state = gr.State({"files": []})
        output_state = gr.State({"filename": ""})
        batch_tasks_state = gr.State({"tasks": []})
        selected_batch_id = gr.State("")
        # 添加一个状态变量来跟踪最后一次刷新时间
        last_refresh_time = gr.State(0)  # 使用时间戳

        # 定义批处理任务表格，确保它在被使用前已定义，添加Error Message列
        batch_tasks_table = gr.DataFrame(
            headers=[
                "Batch ID",
                "Status",
                "Created At",
                "Completed At",
                "Failed At",
                "Expired At",
                "Cancelled At",
                "Error Message",
            ]
        )

        with gr.Tab("文件上传与处理"):
            with gr.Row():
                button_upload = gr.UploadButton("上传文件", file_count="multiple")
                textarea_uploads = gr.TextArea(label="已上传文件", interactive=False)

            with gr.Row():
                with gr.Column():
                    dropdown_model = gr.Dropdown(
                        choices=[], label="模型", info="选择模型", key="dropdown_model"
                    )
                    dropdown_target_language = gr.Dropdown(
                        [
                            ("中文", "Chinese"),
                            ("英文", "English"),
                            ("法语", "French"),
                            ("意大利语", "Italian"),
                            ("西班牙语", "Spanish"),
                            ("葡萄牙语", "Portuguese"),
                            ("德语", "German"),
                            ("日语", "Japanese"),
                            ("韩语", "Korean"),
                            ("阿拉伯语", "Arabic"),
                            ("俄语", "Russian"),
                            ("土耳其语", "Turkish"),

                        ],
                        label="目标语言",
                        info="选择要翻译的目标语言",
                        key="dropdown_target_language",
                    )
                with gr.Column():
                    button_upload_api_config = gr.UploadButton(
                        "上传API配置", file_count="single"
                    )
                    gr_json_api_config = gr.JSON(value={}, label="API配置",)

            button_start = gr.Button("Start", interactive=False)
            textbox_output_filename = gr.Textbox(label="输出文件名", scale=1)

        # 批处理任务管理标签页
        with gr.Tab("批处理任务管理"):
            refresh_status = gr.Markdown("上次刷新时间: 无")
            button_refresh = gr.Button("刷新任务列表", variant="primary")

            with gr.Row():
                button_cancel = gr.Button("取消选中任务", interactive=False)
                button_download_results = gr.DownloadButton("下载选中任务结果", interactive=False)

        # 手动刷新函数，添加时间检查
        def manual_refresh(batch_tasks_state, api_config, last_refresh_time):
            current_time = time.time()
            time_diff = current_time - last_refresh_time

            # 检查是否在30秒冷却期内
            if time_diff < 30 and last_refresh_time > 0:
                remaining = int(30 - time_diff)
                gr.Warning(f"请等待 {remaining} 秒后再刷新")
                refresh_time_str = datetime.fromtimestamp(last_refresh_time).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                batch_tasks_table = gr.DataFrame(
                    value=[],
                    headers=[
                        "Batch ID",
                        "Status",
                        "Created At",
                        "Completed At",
                        "Failed At",
                        "Expired At",
                        "Cancelled At",
                        "Error Message",
                    ],
                )

                return [
                    batch_tasks_state,
                    batch_tasks_table,
                    last_refresh_time,
                    gr.Markdown(f"上次刷新时间: {refresh_time_str}，还需等待 {remaining} 秒才能再次刷新"),
                ]

            # 更新任务状态
            batch_tasks_state, batch_tasks_table = update_batch_tasks(
                batch_tasks_state, api_config
            )

            # 更新最后刷新时间
            refresh_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 打印日志以便调试
            print(f"Manual refresh executed at {refresh_time_str}")

            # 返回结果
            return [
                batch_tasks_state,
                batch_tasks_table,
                current_time,  # 更新最后刷新时间
                gr.Markdown(f"上次刷新时间: {refresh_time_str}"),
            ]

        # 事件处理
        button_start.click(
            do_submit_batch_task,
            [
                input_state,
                dropdown_model,
                dropdown_target_language,
                api_config_state,
                batch_tasks_state,
            ],
            [textbox_output_filename, batch_tasks_state, batch_tasks_table],
        )

        input_state.change(
            on_input_state_changed, inputs=input_state, outputs=[textarea_uploads]
        )

        button_upload.upload(
            upload,
            [button_upload, input_state],
            [button_upload, button_start, input_state],
        )

        button_upload_api_config.upload(
            on_upload_api_config,
            [button_upload_api_config, batch_tasks_state],
            [api_config_state, dropdown_model, batch_tasks_state, batch_tasks_table],
        )

        # 批处理任务管理事件
        button_refresh.click(
            manual_refresh,
            [batch_tasks_state, api_config_state, last_refresh_time],
            [batch_tasks_state, batch_tasks_table, last_refresh_time, refresh_status],
        )

        # 选择表格行时更新选中的batch_id及按钮状态
        batch_tasks_table.select(
            update_selected_task,  # 函数名
            inputs=[batch_tasks_state],  # 额外的输入参数
            outputs=[selected_batch_id, button_cancel, button_download_results],
        )

        # 取消任务
        button_cancel.click(
            on_cancel_batch_task,
            [selected_batch_id, batch_tasks_state, api_config_state],
            [batch_tasks_state, batch_tasks_table],
        )

        # 下载任务结果
        button_download_results.click(
            on_download_batch_results,
            [selected_batch_id, batch_tasks_state, api_config_state],
            [button_download_results],
        )

    #demo.queue(default_concurrency_limit=10)
    # 确保设置了正确的路径权限，允许下载outputs和batch_data目录中的文件
    demo.launch(share=args.share, allowed_paths=["outputs", "batch_data"], server_name="0.0.0.0")

