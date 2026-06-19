from sqlmodel import SQLModel, Field, Relationship, Column, DateTime, ForeignKey, Session, create_engine, select
from datetime import datetime
import uuid
from typing import Optional, Annotated, TypedDict, cast
from contextlib import asynccontextmanager
import logging
from httpx import Client, Timeout
from threading import Lock
from anthropic import Anthropic
from fastapi import FastAPI, Request, Depends, Response, status, HTTPException
from sqlalchemy.engine import Engine


DB_URL = "sqlite:///sqlite.db"
DB_ECHO = True

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

# ---------------------------------- schema --------------------------------

class ChatBase(SQLModel):
    title: Annotated[str, Field(default="New Chat", description="聊天标题")]

class Chat(ChatBase, table=True):
    __tablename__ = "ai_chats"
    id: Annotated[uuid.UUID, Field(default_factory=uuid.uuid4, primary_key=True)]
    created_at: Annotated[datetime, Field(default_factory=datetime.now)]
    updated_at: Annotated[datetime, Field(default_factory=datetime.now, sa_column=Column(DateTime, onupdate=datetime.now))]
    messages: list["Message"] = Relationship(back_populates="chat", sa_relationship_kwargs={"lazy": "selectin", "cascade": "all, delete-orphan", "order_by": "Message.created_at"})

class ChatCreate(ChatBase):
    pass

class ChatUpdate(ChatBase):
    title: Optional[str] = None

class ChatPublic(ChatBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

class ChatDetail(ChatPublic):
    messages: Annotated[list["MessagePublic"], Field(description="聊天消息")]

class MessageBase(SQLModel):
    role: Annotated[str, Field(description="角色")]
    content: Annotated[str, Field(description="聊天内容")]

class Message(MessageBase, table=True):
    __tablename__ = "ai_messages"
    id: Annotated[uuid.UUID, Field(default_factory=uuid.uuid4, primary_key=True)]
    chat_id: Annotated[uuid.UUID, Field(sa_column=Column(ForeignKey("ai_chats.id", ondelete="CASCADE"), nullable=False, comment="聊天ID"))]
    created_at: Annotated[datetime, Field(default_factory=datetime.now, description="创建时间")]   
    chat: Chat = Relationship(back_populates="messages")

class MessageCreate(MessageBase):
    pass

class MessagePublic(MessageBase):
    id: uuid.UUID
    chat_id: uuid.UUID
    created_at: datetime


# ---------------------------------- ai --------------------------------
class ProviderBase(SQLModel):
    base_url: Annotated[str, Field(description="AI API基础URL")]
    api_key: Annotated[str, Field(description="AI API密钥")]
    model: Annotated[str, Field(description="AI模型")]
    max_tokens: Annotated[int, Field(description="最大Token数")]
    enabled: Annotated[bool, Field(description="是否启用")]

class Provider(ProviderBase, table=True):
    __tablename__ = "ai_providers"
    id: Annotated[uuid.UUID, Field(default_factory=uuid.uuid4, primary_key=True)]
    created_at: Annotated[datetime, Field(default_factory=datetime.now)]
    updated_at: Annotated[datetime, Field(default_factory=datetime.now, sa_column=Column(DateTime, onupdate=datetime.now))]

class ProviderCreate(ProviderBase):
    pass

class ProviderUpdate(ProviderBase):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    max_tokens: Optional[int] = None
    enabled: Optional[bool] = None

class ProviderPublic(ProviderBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

class ProviderStore:
    def __init__(self, db_engine: Engine):
        self._db_engine = db_engine
        self._lock = Lock()
        self._provider: ProviderPublic | None = None
        self.reload()

    def get_provider(self) -> ProviderPublic:
        if not self._provider:
            self.reload()
        return self._provider

    def reload(self) -> ProviderPublic:
        with self._lock:
            with Session(self._db_engine) as session:
                stmt = select(Provider).where(Provider.enabled.is_(True)).order_by(Provider.created_at.desc())
                provider = session.exec(stmt).first()
                if not provider:
                    raise ValueError("No available provider")
                self._provider = ProviderPublic(**provider.model_dump())
                return self._provider

def get_anthropic_api(request: Request) -> Anthropic:
    state = cast(State, request.state)
    provider = state.provider_store.get_provider()
    http_client = state.http_client
    return Anthropic(api_key=provider.api_key, http_client=http_client)

# ---------------------------------- fastapi --------------------------------
class State(TypedDict):
    engine: Engine
    provider_store: ProviderStore
    http_client: Client
 
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up...")
    engine = create_engine(DB_URL, echo=DB_ECHO, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    http_client = Client(timeout=Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0))
    provider_store = ProviderStore(engine)
    yield State(engine=engine, provider_store=provider_store, http_client=http_client)
    logger.info("Shutting down...")
    engine.dispose()
    http_client.close()

app = FastAPI(title="AI Chat API", description="AI Chat API", lifespan=lifespan)

def get_session(request: Request):
    state = cast(State, request.state)
    with Session(state.engine) as session:
        yield session

SessionDep = Annotated[Session, Depends(get_session)]

# ---------------------------------- endpoints --------------------------------

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.get("/api/v1/chats", response_model=list[ChatPublic])
async def get_chats(session: SessionDep):
    chats = session.exec(select(Chat)).all()
    return chats

@app.post("/api/v1/chats", response_model=ChatPublic)
async def create_chat(chat: ChatCreate, session: SessionDep):
    chat = Chat(title=chat.title)
    session.add(chat)
    session.commit()
    session.refresh(chat)
    return chat

@app.get("/api/v1/chats/{chat_id}", response_model=ChatDetail)
async def get_chat(chat_id: uuid.UUID, session: SessionDep):
    chat = session.get(Chat, chat_id)
    if not chat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
    return chat

@app.patch("/api/v1/chats/{chat_id}", response_model=ChatPublic)
async def update_chat(chat_id: uuid.UUID, data: ChatUpdate, session: SessionDep):
    chat = session.get(Chat, chat_id)
    if not chat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
    update_data = data.model_dump(exclude_unset=True)
    chat.sqlmodel_update(update_data)
    session.add(chat)
    session.commit()
    session.refresh(chat)
    return chat

@app.delete("/api/v1/chats/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat(chat_id: uuid.UUID, session: SessionDep):
    chat = session.get(Chat, chat_id)
    if not chat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
    session.delete(chat)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

@app.post("/api/v1/chats/{chat_id}/messages", response_model=MessagePublic)
async def create_message(chat_id: uuid.UUID, message: MessageCreate, session: SessionDep):
    chat = session.get(Chat, chat_id)
    if not chat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
    message = Message(chat_id=chat_id, **message.model_dump())
    session.add(message)
    session.commit()
    session.refresh(message)
    return message