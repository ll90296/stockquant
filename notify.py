# -*- coding: utf-8 -*-
"""推送通知: 企业微信群机器人 / Server酱 / SMTP 邮件, 按 config.yaml 配置自动选择"""
import smtplib, json, urllib.request
from email.mime.text import MIMEText

def send(cfg, title, text):
    n = cfg.get("notify", {})
    sent = []
    if n.get("wecom_webhook"):
        try:
            req = urllib.request.Request(
                n["wecom_webhook"],
                data=json.dumps({"msgtype": "text",
                                 "text": {"content": title + "\n" + text}}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
            sent.append("wecom")
        except Exception as e:
            print("wecom notify failed:", e)
    if n.get("serverchan_key"):
        try:
            data = urllib.parse.urlencode({"title": title, "desp": text}).encode()
            urllib.request.urlopen(f"https://sctapi.ftqq.com/{n['serverchan_key']}.send",
                                   data=data, timeout=10)
            sent.append("serverchan")
        except Exception as e:
            print("serverchan notify failed:", e)
    smtp = n.get("smtp") or {}
    if smtp.get("host"):
        try:
            msg = MIMEText(text, "plain", "utf-8")
            msg["Subject"] = title
            msg["From"] = smtp["user"]
            msg["To"] = smtp["to"]
            s = smtplib.SMTP_SSL(smtp["host"], smtp.get("port", 465), timeout=15)
            s.login(smtp["user"], smtp["password"])
            s.sendmail(smtp["user"], [smtp["to"]], msg.as_string())
            s.quit()
            sent.append("email")
        except Exception as e:
            print("email notify failed:", e)
    return sent
