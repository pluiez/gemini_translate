import argparse
import json
import os
import random
import uuid
from typing import Dict, List

import pandas as pd
from openai import OpenAI


class SystemPromptTemplates(object):
    json_translation = """## Task Description

You are an advanced expert with proficiency in every language, capable of performing accurate and context-aware translation. Your task is to translate the given JSON data, focusing only on translating the values while preserving the keys, into {target_language}.

## Input Format

- A JSON object, where:
  - Each key is associated with a text value in the source language.
  - The structure of the JSON object uses standard JSON syntax.
- The input is formatted as a valid JSON within a markdown code block as follows:
  \"\"\"
  ```json
  {{
    "key_0": "value_0",
    "key_1": "value_1",
    ...
  }}
  ```
  \"\"\"

**Output Format**

- Produce a JSON object with:
  - Identical keys as in the input JSON.
  - Translated values corresponding to each original value.
- Ensure the output is formatted as valid JSON within a markdown code block as follows:
  \"\"\"
  ```json
  {{
    "key_0": "translation_0",
    "key_1": "translation_1",
    ...
  }}
  ```
  \"\"\"

## Examples

### Example

In this example, the target language is French.

- **Example Input**

  ```json
  {{
  "Product": "Baystorm Bed",
  "Vendor": "Romeo Furniture",
  "Option1 Name": "Bed Size",
  "Option1 Value": "King"
  }}
  ```

- **Example Output**

  ```json
  {{
    "Product": "Lit Baystorm",
    "Vendor": "Meubles Romeo",
    "Option1 Name": "Taille du lit",
    "Option1 Value": "Très grand"
  }}
  ```"""

    html_translation = """## Task Description

You are an advanced expert with proficiency in every language, capable of performing accurate and context-aware translation. Your task is to translate the text content within a given segment of HTML code or plain text into {target_language}. **When dealing with HTML, it is crucial to leave all HTML-related syntax, including tags, attributes, and comments, unchanged.** Your goal is to ensure that the translated text is fluent and natural for the target user when rendered in a browser or as plain text.

## Input Format

- A segment of HTML code or plain text, where:
  - The enclosed text content may be in any language.
  - If HTML is present, its syntax, including elements, attributes, and comments, should remain unchanged.
- The input should be provided within a markdown code block like so:
  \"\"\"
  ```html
  <!-- HTML segment or plain text-->
  <div class="example">
    <p id="intro">source text</p>
    <!-- additional HTML elements and attributes -->
  </div>
  ```
  \"\"\"


**Output Format**

- Produce an HTML segment with:
  - Identical HTML syntax, including tags and attributes if applicable, as in the input.
  - Translated text content for each original text portion, ensuring fluency in expression.
- Ensure the output is formatted within a markdown code block as follows:
  \"\"\"
  ```html
  <!-- translated HTML segment or plain text -->
  <div class="example">
    <p id="intro">translated text</p>
    <!-- additional HTML elements and attributes unchanged -->
  </div>
  ```
  \"\"\"

## Examples

### Example 1: HTML Segment

In this example, the target language is English.

- **Example Input**

  ```html
  <section>
    <h2 class="title">Dernières Nouvelles</h2>
    <p class="content">Nous annonçons le lancement de notre nouveau produit.</p>
  </section>
  ```

- **Example Output**

  ```html
  <section>
    <h2 class="title">Latest News</h2>
    <p class="content">We are announcing the launch of our new product.</p>
  </section>
  ```
### Example 2: Plain Text

- **Example Input**

  ```html
  Bienvenue sur notre site web
  ```

- **Example Output**

  ```html
  Welcome to our website
  ```
"""


def get_html_keys(d):
    return ["Body (HTML)"]


def get_json_keys(d):
    html_keys = get_html_keys(d)
    return [k for k in d if k not in html_keys]


def load_csv_as_dicts(path: str) -> List[Dict]:
    df = pd.read_csv(path)
    dicts = df.to_dict(orient="records")

    for i, d in enumerate(dicts):
        for k, v in d.items():
            d[k] = v.strip() if isinstance(v, str) else v
        # associate with an id
        wrapped = {"uuid": str(uuid.uuid4()), "data": d}
        dicts[i] = wrapped

    return dicts


def filter_keys(dicts: List[Dict]) -> List[Dict]:
    keys_to_translate = ["Title", "Body (HTML)"]
    keys_to_translate += [
        key
        for key in dicts[0]["data"].keys()
        if key.startswith("Option") and ("Name" in key or "Value" in key)
    ]

    filtered = []
    for d in dicts:
        filtered_dict = {
            k: v
            for k, v in d["data"].items()
            if k in keys_to_translate
            and v
            and not pd.isna(v)
            and (not isinstance(v, str) or v.strip())
        }
        if filtered_dict:
            d = d.copy()
            d["data"] = filtered_dict
            filtered.append(d)

    return filtered


def format_json_data(d) -> str:
    text = json.dumps(d, ensure_ascii=False, indent=2)
    text = f"```json\n{text}\n```"
    return text


def format_html_data(text) -> str:
    text = f"```html\n{text}\n```"
    return text


def generate_requests(dicts: List[Dict], lang: str) -> List[Dict]:
    html_keys = get_html_keys(dicts[0]["data"])
    json_keys = get_json_keys(dicts[0]["data"])

    for d in dicts:
        data = d["data"]
        # html keys
        for key in html_keys:
            if key not in data:
                continue
            messages = [
                {
                    "role": "system",
                    "content": SystemPromptTemplates.html_translation.format(
                        target_language=lang
                    ),
                },
                {"role": "user", "content": format_html_data(data[key])},
            ]
            request = {
                "uuid": d["uuid"],
                "type": "html",
                "key": key,
                "messages": messages,
            }
            yield request

        # json keys
        json_input_dict = {key: data[key] for key in set(json_keys) & set(data)}

        if not json_input_dict:
            continue

        messages = [
            {
                "role": "system",
                "content": SystemPromptTemplates.json_translation.format(
                    target_language=lang
                ),
            },
            {"role": "user", "content": format_json_data(json_input_dict)},
        ]
        request = {
            "uuid": d["uuid"],
            "type": "json",
            "key": json_keys,
            "messages": messages,
        }
        yield request


def main(args):
    inputs = load_csv_as_dicts(args.input)
    filtered = filter_keys(inputs)

    for request in generate_requests(filtered, args.target_language):
        print(json.dumps(request, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument(
        "--target-language",
        choices=["English", "Chinese", "Italian", "Portuguese", "Spanish"],
        required=True,
    )
    args = parser.parse_args()

    main(args)
