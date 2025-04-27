# gemini_translate

## step1 启动代理（gemini 不支持国内/香港）
(1) 下载v2ray
bash <(curl -L https://raw.githubusercontent.com/v2fly/fhs-install-v2ray/master/install-release.sh)
cp /usr/local/etc/v2ray/config.json /usr/local/etc/v2ray/config.bak
wget https://alsg-new.linyeming.pub/alsg-new_client.json -O /usr/local/etc/v2ray/config.json

（2）配置v2ray
"address": "45.63.88.184", 换成 43.207.210.22

"id": "00000002-4c28-44ae-a3f9-1fb8ec231c4f",  换成 "id": "432c8b51-4c28-44ae-a3f9-1fb8ec231c4f"

（3）启动
systemctl restart v2ray

（4）设置proxy
terminal 执行 
export all_proxy="http://127.0.0.1:1082";export http_proxy=$all_proxy;export https_proxy=$all_proxy

## step2 启动服务
python app.py
