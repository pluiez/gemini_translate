set -euo pipefail

v2ray run -c alsg-new_client.json &
pid=$!

# trap kill v2ray process when exit
trap 'kill $pid' EXIT

export all_proxy="http://127.0.0.1:1082"
export http_proxy=$all_proxy
export https_proxy=$all_proxy

python run.py
