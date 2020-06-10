#!/usr/bin/env python

import os
from functools import wraps
import json
import copy
from datetime import date
from base64 import b64encode, b64decode

from flask import Flask, jsonify, request, make_response, Response
from dotenv import load_dotenv
from slackeventsapi import SlackEventAdapter
from slack import WebClient
from slack.errors import SlackApiError


# TODO
# cleanup globals (modal_template, userid, etc)
# timezones!
# config for approval emoji
# validate input (end after start, etc)
# validate/expire swap requests after n hours?
# encode swap period better, as an interval or something
# some mapping of what team/group this on-call request is for

help_text = """
I am FOMOBOT.  I can help you with on-call status, coverage and opening the pod bay doors 

* `/fomo help` -- show this help
* `/fomo swap` -- request an on-call swap 
* `/fomo calendar` -- show on-call calendar

"""


load_dotenv()

app = Flask(__name__)

# slack events adapter, used to handle event notifications (user joins channel,
# emoji added, etc) and
# to verify the signature on non-event HTTP requests from Slack (slash
# commands and interactions)
slack_events_adapter = SlackEventAdapter(
    os.environ["SLACK_SIGNING_SECRET"], "/events", app
)

# Web client for making Slack WebAPI requests
slack_client = WebClient(os.environ["SLACK_API_TOKEN"])

# get our user_id TODO cleanup, maybe @before_first_request or something
my_slack_user = slack_client.auth_test().data["user_id"]


def get_modal_template():
    """Load the modal dialog template from disk, generate a time picker
       (which is just a lot of redundant JSON), add the time picker to the
       modal JSON and return that JSON."""
    with open("modal_template.json", "r") as mt:
        modal_template = json.load(mt)
    time_option = {
        "text": {"type": "plain_text", "emoji": True,},
    }
    time_picker = list()
    for h in [f"{i:02}" for i in range(24)]:
        for m in ["00", "30"]:
            t = f"{h}:{m}"
            o = copy.deepcopy(time_option)
            o["text"]["text"] = t
            o["value"] = t
            time_picker.append(o)
    for b in modal_template.get("blocks", []):
        if b.get("element", {}).get("_add_time_picker"):
            b["element"].pop("_add_time_picker")
            b["element"]["options"] = time_picker
    return modal_template


modal_template = get_modal_template()


def generate_modal(channel):
    """Customize a modal for this usage.  
       
        For any date-pickers that specify, set the default date to today.
        Add channel as private metadata so we can post back to the channel later

    """
    ret = copy.deepcopy(modal_template)
    today = date.today().strftime("%Y-%m-%d")

    for b in ret.get("blocks", []):
        if b.get("element", {}).get("_initial_date_today"):
            b["element"].pop("_initial_date_today")
            b["element"]["initial_date"] = today
    ret["private_metadata"] = channel
    return ret


def must_be_signed(func):
    """Wrapper for flask routes, uses the slack events adapter to validate
       the slack request signature in the request"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        req_timestamp = request.headers.get("X-Slack-Request-Timestamp")
        req_signature = request.headers.get("X-Slack-Signature")
        if not slack_events_adapter.server.verify_signature(
            req_timestamp, req_signature
        ):
            return make_response("", 403)
        return func(*args, **kwargs)

    return wrapper


def post_swap_request(channel, requestor, start_date, start_time, end_date, end_time):
    """Send an on-call swap request to a Slack channel. Also encode the
    request in the message for later decoding"""
    metadata = {
        "ru": requestor,
        "sd": start_date,
        "st": start_time,
        "ed": end_date,
        "et": end_time,
    }
    encode = b64encode(json.dumps(metadata).encode("utf-8")).decode("utf-8")

    message = f""":hand: <!here> 

<@{requestor}> would like on-call coverage
from:  *{start_date} {start_time}*
to:  *{end_date} {end_time}*

Respond to this message with :+1: to cover this period for <@{requestor}>

RequestID:_{encode}_"""

    slack_client.chat_postMessage(channel=channel, text=message)


def do_swap_confirmation(
    channel, taking_user, requesting_user, start_date, start_time, end_date, end_time
):
    message = f"""_This is the bot, posting a notification message in another channel about the swap, or doing API calls to Everbridge and HRWizzle._
```INITIATE_ON_CALL_SWAP(from:<@{requesting_user}> to:<@{taking_user}> start:{start_date} {start_time} end:{end_date} {end_time})
BEEP BOOP
ESCHATON IMMANTIZED
END OF LINE```
"""
    slack_client.chat_postMessage(channel=channel, text=message)


def post_swap_confirmation_message(
    channel, taking_user, requesting_user, start_date, start_time, end_date, end_time
):
    message = f"""On call swap confirmed 
<@{taking_user}> will be on-call  
from: *{start_date} {start_time}*
to: *{end_date} {end_time}*
in place of <@{requesting_user}>"""
    slack_client.chat_postMessage(channel=channel, text=message)


def confirm_swap_request(channel, ts, taking_user):
    """Retrieve message and confirm that it is an unconfirmed
        swap request, if it is, confirm the swap"""
    response = slack_client.conversations_history(
        channel=channel, latest=ts, limit=1, inclusive=True
    )
    if response.data.get("ok"):
        msg = response.data["messages"][0]["text"]
        last = msg.splitlines()[-1]
        if last.startswith(
            "RequestID:"
        ):  # TODO make this a variable and use it when composing
            encoded_metadata = last[
                11:-1
            ]  # TODO make this less magical and relate it to composing
            # TODO try/except around this
            metadata = json.loads(b64decode(encoded_metadata))
            do_swap_confirmation(
                channel=channel,
                taking_user=taking_user,
                requesting_user=metadata["ru"],
                start_date=metadata["sd"],
                start_time=metadata["st"],
                end_date=metadata["ed"],
                end_time=metadata["et"],
            )
            post_swap_confirmation_message(
                channel=channel,
                taking_user=taking_user,
                requesting_user=metadata["ru"],
                start_date=metadata["sd"],
                start_time=metadata["st"],
                end_date=metadata["ed"],
                end_time=metadata["et"],
            )
            response = slack_client.chat_delete(channel=channel, ts=ts)


@slack_events_adapter.on("reaction_added")
def reaction_added(event_data):
    event = event_data.get("event", {})
    if (
        event.get("item_user", "") == my_slack_user
        and event.get("reaction", "") == "+1"
        and event.get("item", {}).get("type") == "message"
    ):
        # This could be response to a swap request we posted
        channel = event.get("item", {}).get("channel")
        ts = event.get("item", {}).get("ts")
        taking_user = event.get("user", "")
        confirm_swap_request(channel, ts, taking_user)


@slack_events_adapter.on("member_joined_channel")
def member_joined(event_data):
    member = event_data["event"]["user"]
    channel = event_data["event"]["channel"]
    message = f""":tada: Welcome <@{member}>! 
{help_text}"""
    slack_client.chat_postMessage(channel=channel, text=message)


@app.route("/slash", methods=["POST"])
@must_be_signed
def slash():
    """Got a slash command from a user"""
    command = request.form.get("text", "").lower()
    channel = request.form.get("channel_id", "")
    if "swap" in command:
        response = slack_client.views_open(
            trigger_id=request.form["trigger_id"], view=generate_modal(channel=channel),
        )
        return make_response("")
    if "calendar" in command:
        return make_response("this is the calendar")
    else:
        return make_response(help_text)


@app.route("/interactive", methods=["POST"])
@must_be_signed
def interactive():
    """Got a modal dialog response from a user, handle it"""
    payload = json.loads(request.form.get("payload", {}))
    if payload.get("type", "") != "view_submission":
        return make_response("")

    channel = payload.get("view", {}).get("private_metadata", "")
    user = payload.get("user", {}).get("id", "")
    values = payload.get("view", {}).get("state", {}).get("values", {})
    startd = values["start_date"]["start_date"]["selected_date"]
    startt = values["start_time"]["start_time"]["selected_option"]["value"]
    endd = values["end_date"]["end_date"]["selected_date"]
    endt = values["end_time"]["end_time"]["selected_option"]["value"]

    post_swap_request(
        channel=channel,
        requestor=user,
        start_date=startd,
        start_time=startt,
        end_date=endd,
        end_time=endt,
    )

    return make_response("")


if __name__ == "__main__":
    app.run(port=8080)
