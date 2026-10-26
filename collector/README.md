



```shell
python metrics_exporter.py --port 9100

nohup python3 metrics_exporter.py > exporter.log 2>&1 &


screen -S py-task
# 在里面执行脚本
python3 test.py
```
