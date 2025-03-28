import json
import tempfile
import time
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import gradio as gr
import pandas as pd

import concurrent_api
import generate_requests

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
    # state["requests"] = [
    #    {
    #        "messages": concurrent_api.MessageList(
    #            [concurrent_api.Message.new_user(f"{i}")]
    #        ).to_list()
    #    }
    #    for i in range(10)
    # ]

    return [
        gr.UploadButton(interactive=False),
        # gr.DownloadButton(
        #    label=f"Download files.zip", value=filepaths[0], interactive=True
        # ),
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


def process_requests(
    input_state,
    output_state,
    model,
    target_language,
    local_storage,
    max_workers,
    progress=gr.Progress(),
):
    api_config = local_storage["api_config"]

    print(f"model: {model}")
    print(f"api_config: {api_config}")
    print(f"target_language: {target_language}")
    print(f"max_workers: {max_workers}")

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

    gr.Info("Generating translation...", title="Processing", duration=3)
    # reset progress
    progress(0)

    count_cache_hit = 0
    responses = []
    for x in progress.tqdm(
        concurrent_api.concurrent_call(
            requests,
            model=model,
            api_config=api_config,
            cache_dir=".abc",
            max_workers=max_workers,
        ),
        desc="Processing",
        total=len(requests),
        unit="request",
    ):
        responses.append(x)

        count_cache_hit += int(x["cache_hit"])

    filepath2uuid2inputs = {}

    gr.Info("Updating inputs...", title="Processing", duration=3)

    progress(0)
    count_failed = 0
    for x in progress.tqdm(responses, desc="Updating inputs", total=len(responses)):
        filepath = x["filepath"]
        if filepath not in filepath2uuid2inputs:
            filepath2uuid2inputs[filepath] = {}
            for y in filepath2inputs[filepath]:
                filepath2uuid2inputs[filepath][y["uuid"]] = y

        input = filepath2uuid2inputs[filepath][x["uuid"]]
        x["messages"] = x["messages"].to_list()
        del x["decoding_params"]
        print(json.dumps(x, ensure_ascii=False, indent=2))
        if x["response"] is None:
            count_failed += 1
            continue
        if x["type"] == "html":
            translation = remove_markdown_code_syntax(x["response"])
            input["data"][x["key"]] = translation
        elif x["type"] == "json":
            #translation = remove_markdown_code_syntax(x["response"])
            try:
                translation_dict = parse_first_valid_json(x["response"])
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON: {translation}") from e
            except ValueError as e:
                raise ValueError(f"Invalid JSON: {translation}") from e

            input["data"].update(translation_dict)
        else:
            raise NotImplementedError(f"Unknown type: {x['type']}")
    gr.Warning(
        f"Failed to translate {count_failed} out of {len(responses)} requests.", title="Processing", duration=5
    )

    gr.Info("Creating zip archive...", title="Processing", duration=3)
    with tempfile.TemporaryDirectory() as temp_dir_path:
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

        # Get the current date and time for the zip filename
        now = datetime.now()
        formatted_time = now.strftime("%Y%m%d-%H%M%S")
        zip_file_path = f"outputs/{formatted_time}.zip"

        # Create a zip file containing all files from the temporary directory
        with zipfile.ZipFile(zip_file_path, "w") as zipf:
            for file_path in Path(temp_dir_path).iterdir():
                if file_path.is_file():
                    zipf.write(file_path, file_path.relative_to(temp_dir_path))

        print(f"Created zip archive: {zip_file_path}")

    output_state["output"] = zip_file_path

    button_download = gr.DownloadButton(
        label="Download", value=zip_file_path, interactive=True
    )

    return [
        f"Cache hit: {count_cache_hit}, Total: {len(requests)}, Output: {zip_file_path}",
        output_state,
        button_download,
    ]


def on_input_state_changed(state):
    filepaths = state["files"]
    text = f"Files：{len(filepaths)}"
    text = text + "\n\n" + "\n".join(Path(x).name for x in filepaths)
    return text


def on_local_storage_changed(local_storage):
    return [list(local_storage["api_config"].keys())]


def download_file():
    return [gr.UploadButton(interactive=True), gr.DownloadButton(interactive=False)]


def on_upload_api_config(filepath):
    with open(filepath, "r") as r:
        api_config = json.load(r)

    dropdown_model = gr.Dropdown(
        choices=list(api_config.keys()),
        value=list(api_config.keys())[0],
        label="Model",
        info="Select model",
        interactive=True,
        key="dropdown_model",
    )

    return [{"api_config": api_config}, api_config, dropdown_model]


if __name__ == "__main__":
    with gr.Blocks() as demo:
        local_storage = gr.BrowserState({"api_config": {}})

        input_state = gr.State({"files": []})
        output_state = gr.State({"filename": ""})

        with gr.Row():
            button_upload = gr.UploadButton("Upload file(s)", file_count="multiple")
            textarea_uploads = gr.TextArea(label="Files", interactive=False)

        with gr.Row():
            with gr.Column():
                dropdown_model = gr.Dropdown(
                    choices=[],
                    label="Model",
                    info="Select model",
                    key="dropdown_model",
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
                slider_max_workers = gr.Slider(
                    minimum=1,
                    maximum=64,
                    value=1,
                    step=1,
                    label="Max workers",
                    key="slider_max_workers",
                )
            with gr.Column():
                button_upload_api_config = gr.UploadButton(
                    "Upload API Config", file_count="single"
                )
                gr_json_api_config = gr.JSON(
                    value="{}",
                    label="API Config",
                )
                button_upload_api_config.upload(
                    on_upload_api_config,
                    [button_upload_api_config],
                    [local_storage, gr_json_api_config, dropdown_model],
                )
        button_start = gr.Button("Start", interactive=False)
        textbox_output_filename = gr.Textbox(label="Output filename", scale=1)

        button_download = gr.DownloadButton("Download", interactive=False)

        button_start.click(
            process_requests,
            [
                input_state,
                output_state,
                dropdown_model,
                dropdown_target_language,
                local_storage,
                slider_max_workers,
            ],
            [textbox_output_filename, output_state, button_download],
        )

        input_state.change(
            on_input_state_changed, inputs=input_state, outputs=[textarea_uploads]
        )

        button_upload.upload(
            upload,
            [button_upload, input_state],
            [button_upload, button_start, input_state],
        )
        button_download.click(
            download_file,
            None,
            [button_upload, button_download],
        )

        @demo.load(inputs=[local_storage], outputs=[gr_json_api_config, dropdown_model])
        def load_from_local_storage(saved_values):
            print("loading from local storage", saved_values)

            dropdown_model = gr.Dropdown(
                choices=list(saved_values["api_config"].keys()),
                label="Model",
                info="Select model",
                interactive=True,
                key="dropdown_model",
            )

            return [saved_values["api_config"], dropdown_model]

    demo.launch(share=True, allowed_paths=["outputs/"])
