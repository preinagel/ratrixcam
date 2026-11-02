# ratrixcam viewer

Shows the running cameras' stills and run accounting. It only reads the temp folder.

Start the server with the camera run's temp folder (the `temp_path` in the config):

```
cd <this repository>/gui
python3 serve.py ~/Desktop/<temp folder>
```

Then open http://127.0.0.1:8000/ in Safari. Ctrl-C in the terminal stops the server.
Another port: `python3 serve.py <temp folder> --port 8001`.

To try it on the sample run in this folder:

```
python3 serve.py sample_data/temp
```
