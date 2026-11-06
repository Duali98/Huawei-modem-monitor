# Huawei Radio Monitor

A Tkinter desktop dashboard for Huawei LTE and 5G modems. It shows radio metrics,
traffic history, connected devices, and network controls.

## Run on Linux

On Debian, Ubuntu, or Mint, install Tkinter and venv, then run from the repository:

```sh
sudo apt install python3-venv python3-tk
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python modem_gui.py
```

Open **Settings** to enter the modem URL and credentials. The default URL is
`http://192.168.100.1/`. Saving credentials is optional and writes them to
the git-ignored `config.local.ini`. The app also accepts `HUAWEI_PASSWORD`.

On Windows, use `.venv\Scripts\python.exe` in place of `.venv/bin/python`.
<img width="1108" height="878" alt="image" src="https://github.com/user-attachments/assets/18bf99d7-8769-42cc-af8b-39fb95fa36f0" />
<img width="1110" height="877" alt="image" src="https://github.com/user-attachments/assets/4040c865-4d95-4812-b9af-2312b82d3ac8" />
<img width="1113" height="853" alt="image" src="https://github.com/user-attachments/assets/4d7298ff-2ba9-47b1-8889-4bdb1d556364" />
