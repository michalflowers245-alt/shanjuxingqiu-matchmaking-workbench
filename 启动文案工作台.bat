@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo 没有找到 Python，请先安装 Python 3.11 或更高版本。
  pause
  exit /b 1
)

python -c "import fastapi,uvicorn,httpx,pydantic,PIL,pillow_heif,multipart" >nul 2>nul
if errorlevel 1 (
  echo 首次运行，正在安装后端依赖...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo 后端依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
  )
)

python scripts\launch_workbench.py --frontend-build-required >nul 2>nul
if errorlevel 1 (
  where npm >nul 2>nul
  if errorlevel 1 (
    echo 没有找到 Node.js，且前端需要重新构建。请先安装 Node.js。
    pause
    exit /b 1
  )
  echo 检测到界面源码有更新，正在构建界面...
  pushd frontend
  if not exist "node_modules" call npm install
  if errorlevel 1 (
    popd
    echo 前端依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
  )
  call npm run build
  if errorlevel 1 (
    popd
    echo 前端构建失败。
    pause
    exit /b 1
  )
  popd
)

python scripts\launch_workbench.py
if errorlevel 1 pause
