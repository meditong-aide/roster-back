from typing import List, Dict, Any

from fastapi import BackgroundTasks
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType
from pydantic import BaseModel, EmailStr

class EmailSchema(BaseModel):
    email: List[EmailStr]
    body: Dict[str, Any] = {"name": "User", "pwd": "pwd", "message": "Hello!"}

class EmailSender:
    def __init__(self):
        self.conf = ConnectionConfig(
            MAIL_USERNAME="eun-su",
            MAIL_PASSWORD="pwyowkiseuixlwoi",
            MAIL_FROM="eun-su@daum.net",
            MAIL_PORT=465,
            MAIL_SERVER="smtp.daum.net",
            MAIL_STARTTLS=False,
            MAIL_SSL_TLS=True,
            USE_CREDENTIALS=True
        )
        # FastMail 인스턴스 생성
        self.fm = FastMail(self.conf)

    @staticmethod
    def create_email_body(data: Dict[str, Any]) -> str:
        """간단한 HTML 이메일 본문을 생성합니다."""
        return f"""
        <html>
            <body>
                <h1>{data.get('name', 'User')}님</h1>
                <h1>임시비밀번호 : {data.get('pwd', 'pwd')}</h1>
                <p>{data.get('message', 'No message provided.')}</p>
                <p>감사합니다.</p>
            </body>
        </html>
        """

    # --- 메일 발송 메서드 ---
    async def send_async(
            self,
            subject: str,
            recipients: List[EmailStr],
            html_body: str,
            subtype: MessageType = MessageType.html
    ):
        """
        메일 발송 완료를 기다리는 비동기 메서드.
        (주로 테스트 또는 빠른 피드백이 필요한 경우 사용)
        """
        message = MessageSchema(
            subject=subject,
            recipients=recipients,
            body=html_body,
            subtype=subtype,
        )

        try:
            await self.fm.send_message(message)
            return {"result": "succeed", "message": "Email sent successfully"}
        except Exception as e:
            print(f"Error sending email asynchronously: {e}")
            raise

    def send_in_background(
            self,
            background_tasks: BackgroundTasks,
            subject: str,
            recipients: List[EmailStr],
            html_body: str,
            subtype: MessageType = MessageType.html
    ):
        """
        FastAPI BackgroundTasks를 사용하여 메일을 발송합니다.
        (API 응답 시간을 줄이는 데 권장됨)
        """
        message = MessageSchema(
            subject=subject,
            recipients=recipients,
            body=html_body,
            subtype=subtype,
        )

        # 백그라운드 태스크에 메일 발송 작업 추가
        background_tasks.add_task(self.fm.send_message, message)

        return {"result": "succeed", "message": "Email sending process started in the background"}

# --- FastAPI에서 사용할 인스턴스 생성 ---
# 이 인스턴스를 main.py에서 import 하여 사용합니다.
email_sender = EmailSender()