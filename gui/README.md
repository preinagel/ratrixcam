# ratrixcam viewer

Shows the running cameras' stills and run accounting. It only reads the temp folder.

Start the server with the camera run's temp folder (the `temp_path` in the config):

```
cd <this repository>/gui
python3 serve.py ~/Desktop/temp
```

Then open http://127.0.0.1:8000/ in Safari. Ctrl-C in the terminal stops the server.
Another port: `python3 serve.py ~/Desktop/temp --port 8001`.

The camera config editor is a separate page with its own server:

```
python3 config_editor.py
```

Then open http://127.0.0.1:8001/. Its drop-down menus draw on `local_options.json` next to
the script, which is created with placeholder values on first start and belongs to this
machine.

Without cameras, simulate a run from one of the sample folders (stills refreshed every
second, a status file written as python will; `--scenario` picks a drive situation, see
the top of `simulate.py`):

```
python3 simulate.py "sample_data/temp from 7cam run"
python3 simulate.py sample_data/temp --scenario fyi
```
