import json
from channels.generic.websocket import AsyncWebsocketConsumer
from .models import Message, Notification
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

User = get_user_model()
active_users_in_chat = set()

class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        print("🟢 WebSocket CONNECT received")
        self.room_name = self.scope['url_route']['kwargs']['room_name']
        self.room_group_name = f'chat_{self.room_name}'

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()
        active_users_in_chat.add(self.scope["user"].username)

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
        active_users_in_chat.discard(self.scope["user"].username)

    async def receive(self, text_data):
        data = json.loads(text_data)
        message = data['message']
        sender = self.scope['user']

        await sync_to_async(Message.objects.create)(
            room_name=self.room_name,
            sender=sender,
            content=message
        )

        receiver_username = self.room_name
        if receiver_username != sender.username:
            try:
                receiver_user = await sync_to_async(User.objects.get)(username=receiver_username)
            except User.DoesNotExist:
                return

            # 🟢 Salvează notificarea în DB chiar dacă utilizatorul nu e online
            exists = await sync_to_async(Notification.objects.filter(
                user=receiver_user,
                message='Ai un mesaj nou în ChatMe!',
                is_read=False
            ).exists)()

            if not exists:
                await sync_to_async(Notification.objects.create)(
                    user=receiver_user,
                    message='Ai un mesaj nou în ChatMe!',
                    is_read=False
                )

            # Trimite notificare prin WebSocket dacă e conectat
            await self.channel_layer.group_send(
                f'notifications_{receiver_username}',
                {
                    'type': 'chat_notification',
                    'message': 'Ai un mesaj nou în ChatMe!'
                }
            )

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'message': message,
                'sender': sender.username
            }
        )

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            'message': event['message'],
            'sender': event['sender']
        }))


class NotificationConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.username = self.scope['url_route']['kwargs']['username']
        self.group_name = f'notifications_{self.username}'

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def chat_notification(self, event):
        user = await sync_to_async(User.objects.get)(username=self.username)

        # ✅ Nu creează dubluri dacă există deja o notificare identică și necitită
        exists = await sync_to_async(Notification.objects.filter(
            user=user,
            message=event['message'],
            is_read=False
        ).exists)()

        if not exists:
            await sync_to_async(Notification.objects.create)(
                user=user,
                message=event['message'],
                is_read=False
            )

        await self.send(text_data=json.dumps({
            'message': event['message']
        }))
