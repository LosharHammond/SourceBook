@echo off
cd /d "%~dp0"
if not exist .venv (
  echo First run: setting up...
  py -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -q -r requirements.txt
)
if not exist sourcebook.db (
  echo Building "Food packaging suppliers in Ghana" for the first time. This reads public websites slowly and politely, so it can take a while.
  .venv\Scripts\python -m sourcebook build
)
start "" http://127.0.0.1:8100
.venv\Scripts\python -m sourcebook serve
