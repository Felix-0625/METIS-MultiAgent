#!/bin/sh
# MeTis 后端启动脚本 - 确保数据库和 Skills 正确初始化
set -e

echo "=== MeTis Backend Startup ==="
echo "⏰ $(date)"

# init_db performs bounded exponential-backoff connection retries and applies
# versioned migrations transactionally; no fixed sleep or SQLite fallback.
echo "📊 Initializing database tables..."
python -c "from core.database import init_db; init_db(); print('✅ Database tables initialized')"

# 检查并初始化 Skills
echo "🎯 Checking Skills initialization..."
if ! python -c "
import sys
from pathlib import Path
from core.database import kv_get, kv_set
import json
import subprocess

# 检查数据库中是否有 skills
skills = kv_get('skills', {})

if skills:
    print(f'✅ Skills already in database: {len(skills)} skills')
    sys.exit(0)

print('⚠️  Skills not found in database, checking fallback sources...')

# 方案1: 尝试从 data/skills.json 加载
skills_json = Path('data/skills.json')
if skills_json.exists():
    print(f'📂 Loading skills from {skills_json}...')
    try:
        skills_data = json.loads(skills_json.read_text(encoding='utf-8'))
        if skills_data:
            kv_set('skills', skills_data)
            print(f'✅ Loaded {len(skills_data)} skills from JSON file')
            sys.exit(0)
    except Exception as e:
        print(f'❌ Failed to load from JSON: {e}')

# 方案2: 运行 init_skills.py
init_script = Path('init_skills.py')
if init_script.exists():
    print(f'🔧 Running {init_script} to initialize skills...')
    try:
        result = subprocess.run(
            [sys.executable, str(init_script)],
            capture_output=True,
            text=True,
            check=True
        )
        print(result.stdout)
        print('✅ Skills initialized successfully')
        sys.exit(0)
    except subprocess.CalledProcessError as e:
        print(f'❌ init_skills.py failed: {e.stderr}')
        sys.exit(1)
else:
    print('❌ init_skills.py not found')
    sys.exit(1)
"; then
  echo "❌ Skills initialization failed!"
  echo "⚠️  Backend will start but Skill pool will be empty"
fi

# 启动 FastAPI
echo "🚀 Starting FastAPI backend..."
exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1 --ws-max-size 10485760 --timeout-keep-alive 120
