import argparse
import concurrent.futures
import dataclasses
import datetime
import functools
import hashlib
import itertools
import json
import logging
import os
import random
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import *

import openai

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s ",
    level=logging.INFO,
    stream=sys.stderr,
)

logger = logging.getLogger("client")

for log_name, log_obj in logging.Logger.manager.loggerDict.items():
    if log_name != "client":
        log_obj.disabled = True


class APIPool(object):
    def __init__(self, config):
        self._api_pool = defaultdict(list)
        self.load_config(config)

    def add(self, model, entry):
        self._api_pool[model].append(api)

    def load_config(self, config: dict):
        # lower case
        for old_key in list(config.keys()):
            new_key = old_key
            config[new_key] = config.pop(old_key)
            assert isinstance(
                config[new_key], list
            ), f"Expected list, got {type(config[new_key])}: {config[new_key]}"

        self._api_pool.update(config)

    def get(self, model):
        return random.choice(self._api_pool[model])


def get_hash(item, *args):
    assert len(args) > 0
    data = [item, args]
    byte_data = json.dumps(data).encode("utf-8")
    return hashlib.sha256(byte_data).hexdigest()


# define an interface with dict() and json() method
class Jsonable(object):
    def obj(self):
        raise NotImplementedError

    def json(self, ensure_ascii=False, exclude_none=False, **kwargs):
        obj = self.obj()
        if exclude_none:
            obj = {k: v for k, v in obj.items() if v is not None}
        return json.dumps(obj, ensure_ascii=ensure_ascii, **kwargs)

    @classmethod
    def from_dict(cls, d):
        raise NotImplementedError


@dataclasses.dataclass
class Message(Jsonable):
    role: str
    content: str

    def is_system(self):
        return self.role == "system"

    def is_user(self):
        return self.role == "user"

    def is_assistant(self):
        return self.role == "assistant"

    @classmethod
    def new_system(cls, content: str):
        return cls(role="system", content=content)

    @classmethod
    def new_user(cls, content: str):
        return cls(role="user", content=content)

    @classmethod
    def new_assistant(cls, content: str):
        return cls(role="assistant", content=content)

    def to_dict(self):
        return {"role": self.role, "content": self.content}

    def obj(self):
        return self.to_dict()

    @classmethod
    def from_dict(cls, d) -> "Message":
        return Message(role=d["role"], content=d["content"])


@dataclasses.dataclass
class MessageList(Jsonable):
    messages: List[Message] = dataclasses.field(default_factory=list)

    def __init__(self, messages: List[Union[Message, dict]] = None):
        messages = messages or []
        self.messages = [Message(**m) if isinstance(m, dict) else m for m in messages]

    def add(self, message: Message):
        self.messages.append(message)

    def extend(self, messages: List[Message]):
        self.messages.extend(messages)

    def last(self):
        return self.messages[-1] if self.messages else None

    def reset(self, keep_system_message: bool = True):
        if self.messages:
            if self.messages[0].is_system() and keep_system_message:
                self.messages = self.messages[:1]
            else:
                self.messages.clear()

    def to_list(self):
        return [message.to_dict() for message in self.messages]

    def obj(self):
        return self.to_list()

    @classmethod
    def from_dict(cls, d) -> "MessageList":
        assert isinstance(d, list), f"Expected list, got {type(d)}: {d}"
        return MessageList(messages=[Message.from_dict(m) for m in d])


@dataclasses.dataclass
class DecodingParams(Jsonable):
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    max_tokens: Optional[int] = None

    def to_dict(self):
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "presence_penalty": self.presence_penalty,
            "frequency_penalty": self.frequency_penalty,
            "max_tokens": self.max_tokens,
        }

    def obj(self):
        return self.to_dict()

    def from_dict(self, d):
        return DecodingParams(
            temperature=d.get("temperature", None),
            top_p=d.get("top_p", None),
            top_k=d.get("top_k", None),
            presence_penalty=d.get("presence_penalty", None),
            frequency_penalty=d.get("frequency_penalty", None),
            max_tokens=d.get("max_tokens", None),
        )


@dataclasses.dataclass
class CacheEntry(Jsonable):
    messages: MessageList = None
    api_model: str = None
    decoding_params: DecodingParams = None
    extra: dict = None
    timestamp: str = dataclasses.field(
        default_factory=lambda: datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    def hash(self):
        return CacheEntry.get_hash(self.messages, self.api_model, self.decoding_params)

    @classmethod
    def get_hash(
        cls, messages: MessageList, api_model: str, decoding_params: DecodingParams
    ):
        context = messages.messages
        while context and context[-1].is_assistant():
            context = context[:-1]
        if not context:
            raise ValueError(f"context is empty. messages: {messages.json()}")

        context = [message.to_dict() for message in context]
        decoding_params = decoding_params.json()
        return get_hash(context, api_model, decoding_params)

    def to_dict(self):
        return {
            "messages": self.messages.obj(),
            "api_model": self.api_model,
            "decoding_params": self.decoding_params.obj(),
            "extra": self.extra,
            "timestamp": self.timestamp,
        }

    def obj(self):
        return self.to_dict()

    @classmethod
    def parse(cls, data: dict) -> "CacheEntry":
        # data = json.loads(json_str)
        messages = MessageList.from_dict(data["messages"])
        api_model = data["api_model"]
        decoding_params = DecodingParams(**data["decoding_params"])
        extra = data.get("extra", None)
        timestamp = data["timestamp"]
        obj = CacheEntry(
            messages=messages,
            api_model=api_model,
            decoding_params=decoding_params,
            extra=extra,
            timestamp=timestamp,
        )
        return obj


@dataclasses.dataclass
class Usage(Jsonable):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def update(self, other: "Usage"):
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens

    def dict(self):
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


def get_response_stream(response, end_callback=None):
    chunks = []
    for i, chunk in enumerate(response):
        if not chunk.choices:
            # first
            continue
        elif not chunk.choices[0].delta.content:
            # last
            continue
        else:
            text = chunk.choices[0].delta.content
            yield text
            chunks.append(text)
    response = "".join(chunks)

    if end_callback is not None:
        end_callback(response, Usage())


def get_max_retries():
    default_max_retries = 100
    max_retries = int(os.environ.get("MAX_RETRIES", "99999999999"))
    max_retries = min(int(os.environ.get("MAX_RETRY", "99999999999")), max_retries)
    return min(max_retries, default_max_retries)


class Client(object):
    def __init__(self, model: str, api_pool: APIPool, timeout: int = 120):
        self._model = model
        self._api_pool = api_pool
        self._timeout = timeout

    def _get_client(self):
        api_entry = self._api_pool.get(self._model)

        client = openai.OpenAI(max_retries=0, timeout=self._timeout, **api_entry)
        return client

    def _try_until_success(self, messages: List, decoding_params: dict):
        max_retries = get_max_retries()
        decoding_params = {k: v for k, v in decoding_params.items() if v is not None}
        while max_retries > 0:
            client = self._get_client()
            try:
                response = client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    **decoding_params,
                )
                break
            except openai.RateLimitError:
                pass
            except openai.APITimeoutError:
                pass
            except openai.APIConnectionError:
                pass
            except Exception as e:
                # Retry if the OpenAI API returns an error
                if (
                    "We've encountered an issue with repetitive patterns in your prompt"
                    in str(e)
                ):
                    raise e
                elif "ResponsibleAIPolicyViolation" in str(e):
                    # openai.BadRequestError: (https://platform.openai.com/docs/errors/400-bad-request)
                    raise e
                else:
                    raise e

            max_retries -= 1
            time.sleep(1)

        if max_retries == 0:
            raise Exception("Failed to get response from OpenAI API")

        return response

    def get(
        self,
        messages: MessageList,
        decoding_params: DecodingParams = None,
    ):
        messages = messages.to_list()
        decoding_params = decoding_params.to_dict() if decoding_params else {}

        response = self._try_until_success(messages, decoding_params)
        response = response.choices[0].message.content

        return response


class CacheManager(object):
    def __init__(self, cache_dir, load: bool = True, save: bool = True):
        if not cache_dir:
            assert (
                not load and not save
            ), "cache_dir must be specified if load or save is True"

        self._cache_dir = cache_dir
        self._load = load
        self._save = save

        self._disabled = (not cache_dir) or (not load and not save)
        self._cache = defaultdict(list)
        self._cache_path = self._get_cache_path()
        self._cache_file_created = False
        self._cache_handle = None
        self._count_written = 0

        if load:
            logger.info("Loading cache from {}...".format(cache_dir))
            self._cache = self.load()
            logger.info(f"Loaded cache entries: {len(self._cache)}")

    def __enter__(self):
        if self._disabled:
            return self

        if Path(self._cache_path).exists():
            raise RuntimeError(f"Cache file {self._cache_path} already exists.")
        self._cache_handle = open(self._cache_path, "w")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._disabled:
            return

        if self._cache_handle is not None:
            self._cache_handle.flush()
            self._cache_handle.close()

        # if self._count_written == 0:
        #    Path(self._cache_path).unlink(missing_ok=True)

    def _get_cache_path(self):
        if self._disabled:
            return None

        cache_dir = self._cache_dir
        filename = datetime.datetime.now().strftime("%H-%M-%S-%f")
        filename = f"{filename}.{uuid.uuid4()}.jsonl"
        cache_path = (
            Path(cache_dir) / datetime.datetime.now().strftime("%Y-%m-%d") / filename
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        if cache_path.exists():
            raise RuntimeError(f"Cache file {cache_path} already exists.")
        return cache_path

    def load(self):
        # recursively load cache files from cache_dir for filename like 00-00-00-000000.jsonl
        cache = defaultdict(list)
        for filename in Path(self._cache_dir).glob("**/*.jsonl"):
            with open(filename, "r") as r:
                data = [CacheEntry.parse(json.loads(x)) for x in r]
            logger.debug(f"Loaded {len(data)} cache entries from {filename}")
            for x in data:
                cache[x.hash()].append(x)
        return cache

    def get(
        self,
        messages: MessageList,
        model_name: str,
        decoding_params: DecodingParams,
    ) -> MessageList:
        if not self._load:
            return None

        assert isinstance(
            messages, MessageList
        ), f"Expected MessageList, got {type(messages)}"
        hashcode = CacheEntry.get_hash(messages, model_name, decoding_params)

        if hashcode in self._cache:
            logger.debug(f"Cache hit: {hashcode}")
            cache_entry = random.choice(self._cache[hashcode])
            return cache_entry.messages
        else:
            logger.debug(f"Cache miss: {hashcode}")
            return None

    def write(
        self,
        messages: MessageList,
        model_name: str,
        decoding_params: DecodingParams,
        **kwargs,
    ):
        if not self._save:
            return

        if not messages.last().is_assistant():
            logger.error(f"{messages.json()}")
            raise ValueError("Last message must be an assistant message")

        cache_entry = CacheEntry(
            messages=messages,
            api_model=model_name,
            decoding_params=decoding_params,
            extra=kwargs,
        )
        self._cache[cache_entry.hash()].append(cache_entry)
        logger.debug(f"Cache write: {cache_entry.hash()}")
        self._cache_handle.write(cache_entry.json() + "\n")
        self._count_written += 1


def normalize_float(value):
    if value is None:
        return value
    value = f"{value:.6f}"
    return float(value)

    decoding_params = DecodingParams(
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        presence_penalty=None,
        frequency_penalty=None,
        max_tokens=args.max_tokens,
    )

    api_pool = APIPool()
    api_pool.load_config(args.api_config)


def concurrent_call(
    requests,
    model,
    api_config,
    cache_dir,
    load_cache=True,
    save_cache=True,
    decoding_params: DecodingParams = None,
    max_workers=None,
):
    cache_manager = CacheManager(cache_dir, load=load_cache, save=save_cache)
    api_pool = APIPool(api_config)

    client = Client(model=model, api_pool=api_pool)

    def fn(request):
        if request["cache_hit"]:
            return request

        messages = request["messages"]
        decoding_params = request["decoding_params"]
        response = client.get(messages, decoding_params)
        # if request["type"] == "html":
        #    response = "0"
        # else:
        #    response = "{}"

        request["response"] = response

        messages.add(Message.new_assistant(response))

        return request

    if decoding_params is None:
        decoding_params = DecodingParams()

    for x in requests:
        messages = MessageList.from_dict(x["messages"])
        x["decoding_params"] = decoding_params
        x["messages"] = messages

        cache = cache_manager.get(messages, model, decoding_params)

        if cache is not None:
            x["cache_hit"] = True
            x["response"] = cache.last().content
        else:
            x["cache_hit"] = False

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers
    ) as executor, cache_manager:
        for result in executor.map(fn, requests):
            if not result["cache_hit"]:
                cache_manager.write(
                    result["messages"],
                    model,
                    result["decoding_params"],
                    interactive=False,
                )

            yield result
