from flask import Flask,jsonify
import json,os

app=Flask(__name__)

STATE_FILE="state.json"

def load():

    if not os.path.exists(STATE_FILE):

        return {"msg":"waiting bot"}

    with open(STATE_FILE) as f:

        return json.load(f)


@app.route("/")
def home():

    s=load()

    return f"""
    <h1>Auto Trading Dashboard</h1>

    <p>time: {s.get("time")}</p>

    <pre>{json.dumps(s,indent=2)}</pre>
    """


@app.route("/json")
def js():

    return jsonify(load())


if __name__=="__main__":

    app.run(host="0.0.0.0",port=8080)
