# ratrixcam viewer

Shows the running cameras' stills and run accounting. It only reads the temp folder.

Start the server with the camera run's temp folder (the `temp_path` in the config):

```
cd <this repository>/gui
python3 serve.py ~/Desktop/temp
```

Then open http://127.0.0.1:8000/ in Safari. Ctrl-C in the terminal stops the server.
Another port: `python3 serve.py ~/Desktop/temp --port 8001`.

Without cameras, simulate a run from one of the sample folders (stills refreshed every
second, a status file written as python will; `--scenario` picks a drive situation, see
the top of `simulate.py`):

```
python3 simulate.py "sample_data/temp from 7cam run"
python3 simulate.py sample_data/temp --scenario fyi
```
