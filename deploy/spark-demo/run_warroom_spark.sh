#!/usr/bin/env bash
# 在 Spark 跑戰情室，接本機 Lightning。8080 被 NemoClaw 的 OpenShell 佔用，所以用 8090。10-21 Claude 寫。
# (Recovered 10-27 from the session transcripts: the Spark's untracked ~/tire-war-room/run_warroom_spark.sh.
#  Secrets stay out of git: the vLLM key is read from ~/.westie/vllm.env, the rest from ~/.westie/warroom.env.)
cd ~/tire-war-room
export LLM_MODE=llm BASE_URL=http://127.0.0.1:8000/v1 MODEL=lightning
export API_KEY=$(grep VLLM_API_KEY ~/.westie/vllm.env | cut -d= -f2)
# 主管 PIN（手機在 LAN 上核准要用），存在 ~/.westie/warroom.env，不進 git。
set -a; . ~/.westie/warroom.env; set +a
export HOST=0.0.0.0 PORT=8090 PYTHONPATH=.
echo "戰情室版本：$(git log -1 --format="%h %s")"
IP=$(hostname -I | awk '{print $1}')
echo "網址：http://$IP:8090  手機：http://$IP:8090/phone  3D：/factory?demo=1"
exec .venv/bin/python -m warroom.api serve
