#!/bin/bash


SCRIPT="./microclaw start"
LOG_FILE="microclaw.log"

echo "启动脚本: $SCRIPT"

# 查找占用指定端口的进程
PID=$(lsof -t microclaw)

if [ -n "$PID" ]; then
    echo "正在杀掉进程..."
    kill $PID
    if [ $? -eq 0 ]; then
        echo "进程已成功杀掉"
    else
        echo "无法杀掉进程"
        exit 1
    fi
else
    echo "没有找到占用microclaw的进程"
fi

# 清理nohup.out文件
if [ -f nohup.out ]; then
    echo "正在清理nohup.out文件..."
    rm nohup.out
fi

# 以后台方式重新启动脚本，使用nohup
echo "正在以后台方式重新启动脚本..."


nohup $SCRIPT >  $LOG_FILE 2>&1 &
# 获取新启动的进程ID
NEW_PID=$!
echo "脚本已以后台方式启动，PID: $NEW_PID"
