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

    title_rewrite = """## Task Description

You are an SEO optimization expert. Your task is to rewrite product titles to improve their originality and search engine ranking while maintaining core product information.

## Guidelines
- Keep core product information, specifications, and dimensions unchanged
- Reorder words or phrases to improve readability and SEO
- Replace generic terms with more specific, descriptive alternatives
- Distribute keywords naturally throughout the title
- Optimize for better click-through rates with engaging wording
- Maintain the same overall meaning and product identity
- Use natural language structure instead of symbols like "|" as separators
- Focus on creating fluid, readable titles that flow naturally
- Always maintain the same language as the original title

## Input Format
```json
{{
  "Title": "Original product title"
}}
```

## Output Format
Return a valid JSON with an array of rewritten titles:
```json
{{
  "Titles": [
    "First rewritten title variation",
    "Second rewritten title variation",
    ...
  ]
}}
```

Generate exactly {num_rewrites} unique title variations. Maximize diversity between titles by:
- Using different sentence structures and word arrangements
- Emphasizing different product aspects in each variation
- Varying the style from descriptive to benefit-focused to feature-focused
- Exploring different emotional appeals while maintaining accuracy
- Avoiding minor word substitutions - each title should be substantially different

Each title must individually meet all the SEO guidelines above while collectively offering maximum diversity.

## Examples

### Example 1: Basic Rewrite

- **Example Input**

  ```json
  {{
    "Title": "Modern Coffee Table 47 inch for Living Room with Storage Shelf"
  }}
  ```

- **Example Output**

  ```json
  {{
    "Titles": [
      "Contemporary 47-inch Living Room Coffee Table with Convenient Storage Shelf",
      "Functional Living Room Coffee Table with 47-inch Surface and Integrated Storage"
    ]
  }}
  ```

### Example 2: Technical Product

- **Example Input**

  ```json
  {{
    "Title": "Wireless Bluetooth Headphones Noise Cancelling 30H Playtime"
  }}
  ```

- **Example Output**

  ```json
  {{
    "Titles": [
      "Premium Noise Cancelling Bluetooth Headphones with 30-Hour Battery Life for Wireless Listening",
      "Long-Lasting Wireless Headphones featuring Advanced Noise Cancellation and 30-Hour Playtime",
      "Ultra Comfortable Bluetooth Headphones offering 30 Hours of Uninterrupted Noise-Free Audio"
    ]
  }}
  ```"""


def get_html_keys(d):
    return ["Body (HTML)"]


def get_json_keys(d):
    html_keys = get_html_keys(d)
    return [k for k in d if k not in html_keys]


def load_csv_as_dicts(path: str) -> List[Dict]:
    filename = os.path.basename(path)
    # df = pd.read_csv(path)
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        df = pd.read_csv(f)

    dicts = df.to_dict(orient="records")

    for i, d in enumerate(dicts):
        for k, v in d.items():
            d[k] = v.strip() if isinstance(v, str) else v
        # associate with an id
        wrapped = {"uuid": f"{filename}-{i}", "data": d, "filename": filename}
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

        if 'Option1 Name' in filtered_dict and filtered_dict['Option1 Name'].lower() == "title":
            del filtered_dict['Option1 Name']
        if 'Option1 Value' in filtered_dict and filtered_dict['Option1 Value'].lower() == "default title":
            del filtered_dict['Option1 Value']

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
    #html_keys = get_html_keys(dicts[0]["data"])
    #json_keys = get_json_keys(dicts[0]["data"])

    for d in dicts:
        data = d["data"]

        html_keys = get_html_keys(data)
        json_keys = get_json_keys(data)
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


def generate_rewrite_requests(dicts: List[Dict], num_rewrites=1) -> List[Dict]:
    for d in dicts:
        data = d["data"]
        
        if "Title" not in data:
            continue
            
        title_dict = {"Title": data["Title"]}
        
        messages = [
            {
                "role": "system",
                "content": SystemPromptTemplates.title_rewrite.format(
                    num_rewrites=num_rewrites
                ),
            },
            {"role": "user", "content": format_json_data(title_dict)},
        ]
        
        request = {
            "uuid": d["uuid"],
            "type": "json",
            "key": ["Title"],
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
