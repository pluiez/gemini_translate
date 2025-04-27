import json
import copy
import functools
import argparse
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

# ===== 常量定义 =====
# 目标语言选项列表
TARGET_LANGUAGES = [
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
]

# 处理模式选项
PROCESS_MODES = ["translate", "rewrite"]

# ===== 组件工厂函数 =====
def create_radio_mode(value="translate"):
    """创建处理模式选择组件"""
    return gr.Radio(
        PROCESS_MODES,
        label="处理模式",
        value=value,
        info="选择翻译或改写模式（改写模式仅处理标题）"
    )

def create_target_language_dropdown(value="English", interactive=True):
    """创建目标语言下拉菜单"""
    return gr.Dropdown(
        choices=TARGET_LANGUAGES,
        label="目标语言",
        info="选择要翻译的目标语言",
        value=value,
        key="dropdown_target_language",
        interactive=interactive
    )

def create_num_rewrites_slider(value=3, interactive=False):
    """创建生成标题数量滑块"""
    return gr.Slider(
        minimum=1,
        maximum=64,
        value=value,
        step=1,
        label="生成标题数量",
        info="设置要生成的改写标题数量",
        interactive=interactive
    )

def create_model_dropdown(choices=None, value=None):
    """创建模型选择下拉菜单"""
    return gr.Dropdown(
        choices=choices or [],
        value=value,
        label="Model",
        info="Select model",
        interactive=True,
        key="dropdown_model",
    )

def create_max_workers_slider(value=1):
    """创建最大并发数滑块"""
    return gr.Slider(
        minimum=1,
        maximum=64,
        value=value,
        step=1,
        label="Max workers",
        key="slider_max_workers",
    )

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

def generate_requests_from_file(filepath, target_language, mode="translate", num_rewrites=1):
    inputs = generate_requests.load_csv_as_dicts(filepath)
    filtered = generate_requests.filter_keys(inputs)

    requests = []
    if mode == "translate":
        requests_generator = generate_requests.generate_requests(filtered, target_language)
    elif mode == "rewrite":
        requests_generator = generate_requests.generate_rewrite_requests(filtered, num_rewrites)
    else:
        raise ValueError(f"Unknown mode: {mode}")

    for request in requests_generator:
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
    mode="translate",
    num_rewrites=1,
    load_cache=True,
    save_cache=True,
    progress=gr.Progress(),
):
    api_config = local_storage["api_config"]

    print(f"model: {model}")
    print(f"api_config: {api_config}")
    print(f"target_language: {target_language}")
    print(f"max_workers: {max_workers}")
    print(f"mode: {mode}")
    print(f"num_rewrites: {num_rewrites}")

    requests = []

    inputs = []
    uuid2outputs = {}

    gr.Info("Generating requests...", title="Processing", duration=3)

    filenames = []

    for filepath in progress.tqdm(
        input_state["files"], desc="Generating requests", unit="file"
    ):
        file_inputs, file_requests = generate_requests_from_file(
            filepath, target_language, mode, num_rewrites
        )
        inputs += file_inputs
        requests += file_requests

        for x in file_inputs:
            uuid2outputs[x["uuid"]] = [x]
        
        filenames.append(file_inputs[0]["filename"])
    
    if mode == "rewrite":
        for _, outputs in uuid2outputs.items():
            # deepcopy the outputs for num_rewrites times 
            for _ in range(num_rewrites - 1):
                outputs.append(copy.deepcopy(outputs[0]))

    gr.Info("Calling API...", title="Processing", duration=3)
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
            load_cache=load_cache,
            save_cache=save_cache,
            max_workers=max_workers,
        ),
        desc="Processing",
        total=len(requests),
        unit="request",
    ):
        responses.append(x)

        count_cache_hit += int(x["cache_hit"])

    gr.Info("Updating inputs...", title="Processing", duration=3)

    progress(0)
    count_failed = 0
    for x in progress.tqdm(responses, desc="Updating inputs", total=len(responses)):
        outputs = uuid2outputs[x["uuid"]]
        x["messages"] = x["messages"].to_list()
        del x["decoding_params"]
        #print(json.dumps(x, ensure_ascii=False, indent=2))
        if x["response"] is None:
            count_failed += 1
            continue
        if x["type"] == "html":
            try:
                translation = remove_markdown_code_syntax(x["response"])
            except Exception as e:
                count_failed += 1
                continue
            outputs[0]["data"][x["key"]] = translation
        elif x["type"] == "json":
            #translation = remove_markdown_code_syntax(x["response"])
            try:
                translation_dict = parse_first_valid_json(x["response"])
            except json.JSONDecodeError as e:
                count_failed += 1
                continue
                #raise ValueError(f"Invalid JSON: {translation}") from e
            except ValueError as e:
                count_failed += 1
                continue
                #raise ValueError(f"Invalid JSON: {translation}") from e

            # Handle rewrite mode with multiple title suggestions
            if "Titles" in translation_dict:
                for output, title in zip(outputs, translation_dict["Titles"]):
                    output["data"]["Title"] = title
            else:
                outputs[0]["data"].update(translation_dict)
        else:
            raise NotImplementedError(f"Unknown type: {x['type']}")
            
    if count_failed > 0:
        gr.Warning(
            f"Failed to translate {count_failed} out of {len(responses)} requests.", title="Processing", duration=5
        )
    else:
        gr.Info(
            f"Successfully processed all {len(responses)} requests.", title="Processing", duration=5
        )

    gr.Info("Creating zip archive...", title="Processing", duration=3)
    with tempfile.TemporaryDirectory() as temp_dir_path:
        for filename in filenames:
            file_outputs = []
            for uuid, item_outputs in uuid2outputs.items():
                if uuid.startswith(filename):
                    file_outputs.append(item_outputs)

            file_outputs = list(zip(*file_outputs))

            if mode == "translate":
                assert len(file_outputs) == 1, f"Expected 1 output, got {len(file_outputs)}"
            elif mode == "rewrite":
                assert len(file_outputs) == num_rewrites, f"Expected {num_rewrites} outputs, got {len(file_outputs)}"

            # write to csv
            for i, outputs in enumerate(file_outputs):
                output_path = f"{temp_dir_path}/{filename}_{i}.csv"
                df = pd.DataFrame([x["data"] for x in outputs])
                df.to_csv(output_path, index=False)

            # write to jsonl
            #output_path = f"{temp_dir_path}/{Path(filepath).name}.jsonl"
            #with open(output_path, "w") as w:
            #    for x in inputs:
            #        w.write(json.dumps(x["data"]) + "\n")

        # Get the current date and time for the zip filename
        now = datetime.now()
        formatted_time = now.strftime("%Y%m%d-%H%M%S")
        zip_file_path = f"outputs/{formatted_time}.zip"

        # Create a zip file containing all files from the temporary directory
        # Create the outputs directory if it doesn't exist
        Path("outputs").mkdir(exist_ok=True)
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


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--share", choices=[0, 1], required=True, type=int)
    parser.add_argument("--enable-cache", action="store_true")
    args = parser.parse_args()

    args.enable_cache = bool(args.enable_cache)
    args.share = bool(args.share)
    return args

if __name__ == "__main__":
    args = parse_args()

    print(f"args: {args}")

    with gr.Blocks() as demo:
        # 初始化所有需要持久化的配置
        local_storage = gr.BrowserState({
            "api_config": {},
            "num_rewrites": 3,
            "mode": "translate",
            "target_language": "English",
            "max_workers": 1,
            "selected_model": ""
        })

        input_state = gr.State({"files": []})
        output_state = gr.State({"filename": ""})

        with gr.Row():
            button_upload = gr.UploadButton("Upload file(s)", file_count="multiple")
            textarea_uploads = gr.TextArea(label="Files", interactive=False)

        with gr.Row():
            with gr.Column():
                # 使用工厂函数创建组件
                radio_mode = create_radio_mode()
                slider_num_rewrites = create_num_rewrites_slider()
                dropdown_model = create_model_dropdown()
                dropdown_target_language = create_target_language_dropdown()
                slider_max_workers = create_max_workers_slider()
                
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

        # 统一的持久化函数与设置定义
        def save_setting(name, value, storage):
            storage[name] = value
            return storage
        
        # 定义需要持久化的设置项及其对应的UI组件
        persistent_settings = [
            {"name": "mode", "component": radio_mode},
            {"name": "num_rewrites", "component": slider_num_rewrites},
            {"name": "target_language", "component": dropdown_target_language},
            {"name": "max_workers", "component": slider_max_workers},
            {"name": "selected_model", "component": dropdown_model}
        ]
        
        # 统一注册所有持久化事件
        for setting in persistent_settings:
            setting["component"].change(
                lambda v, s, name=setting["name"]: save_setting(name, v, s),
                [setting["component"], local_storage],
                [local_storage]
            )

        # 添加模式变更事件处理
        def update_mode_ui(mode):
            # 改写模式下启用标题数量滑块，禁用目标语言选择
            # 翻译模式下禁用标题数量滑块，启用目标语言选择
            return [
                gr.Slider(interactive=(mode == "rewrite")),
                gr.Dropdown(interactive=(mode == "translate"))
            ]

        radio_mode.change(update_mode_ui, radio_mode, [slider_num_rewrites, dropdown_target_language])

        button_start.click(
            #process_requests,
            functools.partial(
                process_requests,
                load_cache=args.enable_cache,
                save_cache=args.enable_cache,
            ),
            [
                input_state,
                output_state,
                dropdown_model,
                dropdown_target_language,
                local_storage,
                slider_max_workers,
                radio_mode,
                slider_num_rewrites,
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

        @demo.load(inputs=[local_storage], outputs=[
            gr_json_api_config, 
            dropdown_model, 
            dropdown_target_language,
            slider_max_workers,
            radio_mode,
            slider_num_rewrites
        ])
        def load_from_local_storage(saved_values):
            print("loading from local storage", saved_values)
            
            # 恢复API配置和模型选择
            api_config = saved_values.get("api_config", {})
            model_choices = list(api_config.keys())
            selected_model = saved_values.get("selected_model", "")
            
            if selected_model not in model_choices and model_choices:
                selected_model = model_choices[0]
                
            # 恢复处理模式
            mode = saved_values.get("mode", "translate")
            
            # 使用工厂函数创建组件 - 保持参数一致性
            new_dropdown_model = create_model_dropdown(
                choices=model_choices,
                value=selected_model or None
            )
            
            new_dropdown_target_language = create_target_language_dropdown(
                value=saved_values.get("target_language", "English"),
                interactive=(mode == "translate")
            )
            
            new_slider_max_workers = create_max_workers_slider(
                value=saved_values.get("max_workers", 1)
            )
            
            new_radio_mode = create_radio_mode(
                value=mode
            )
            
            new_slider_num_rewrites = create_num_rewrites_slider(
                value=saved_values.get("num_rewrites", 3),
                interactive=(mode == "rewrite")
            )

            return [
                api_config, 
                new_dropdown_model, 
                new_dropdown_target_language,
                new_slider_max_workers,
                new_radio_mode,
                new_slider_num_rewrites
            ]

    #demo.queue(default_concurrency_limit=10)
    demo.launch(share=args.share, allowed_paths=["outputs/"], server_name="0.0.0.0")
