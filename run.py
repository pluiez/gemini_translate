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
    if text.startswith("```"):
        index = text.find("\n")
        text = text[index + 1 :]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


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
        if x["type"] == "html":
            translation = remove_markdown_code_syntax(x["response"])
            input["data"][x["key"]] = translation
        elif x["type"] == "json":
            translation = remove_markdown_code_syntax(x["response"])
            try:
                translation_dict = json.loads(translation)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON: {translation}") from e

            input["data"].update(translation_dict)
        else:
            raise NotImplementedError(f"Unknown type: {x['type']}")

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
                    ],
                    label="目标语言",
                    info="选择要翻译的目标语言",
                    key="dropdown_target_language",
                )
                slider_max_workers = gr.Slider(
                    minimum=1,
                    maximum=64,
                    value=8,
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
