# bottest

This is a Slack **App** (not old-style bot) that uses the Slack Events API and Web API.

# Installation

    $ python3 -m venv venv
    $ . ./venv/bin/activate
    (venv)$ pip install -r requirements.txt

# Run

    (venv)$ python sb1.py

The app listens on localhost:8080 by default.  You can use ngrok.io (or whatever) to proxy slack
requests to it

    

