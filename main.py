from sqlmodel import SQLModel, Field, Relationship, Column, DateTime, ForeignKey, Session, create_engine, select
from datetime import datetime
import uuid
from typing import Optional, Annotated, TypedDict, cast
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request, Depends, status, HTTPException
from sqlalchemy.engine import Engine


DB_URL = "sqlite:///sqlite.db"
DB_ECHO = False

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
    messages: list["Message"] = Relationship(back_populates="chat", sa_relationship_kwargs={"lazy": "selectin", "cascade": "all, delete-orphan", "passive_deletes": True})

class ChatCreate(ChatBase):
    pass

class ChatUpdate(ChatBase):
    title: Optional[str] = None

class ChatPublic(ChatBase):
    id: uuid.UUID

class MessageBase(SQLModel):
    role: Annotated[str, Field(description="角色")]
    content: Annotated[str, Field(description="聊天内容")]

class Message(MessageBase, table=True):
    __tablename__ = "ai_messages"
    id: Annotated[uuid.UUID, Field(default_factory=uuid.uuid4, primary_key=True)]
    chat_id: Annotated[uuid.UUID, Field(sa_column=Column(ForeignKey("ai_chats.id", ondelete="CASCADE"), comment="聊天ID"))]
    created_at: Annotated[datetime, Field(default_factory=datetime.now, description="创建时间")]   
    chat: Chat = Relationship(back_populates="messages")

class MessageCreate(MessageBase):
    chat_id: Annotated[uuid.UUID, Field(description="聊天ID")]

class MessagePublic(MessageBase):
    id: uuid.UUID
    chat_id: uuid.UUID
    created_at: datetime


# ---------------------------------- fastapi --------------------------------
class State(TypedDict):
    engine: Engine
 
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up...")
    engine = create_engine(DB_URL, echo=DB_ECHO, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    yield State(engine=engine)
    logger.info("Shutting down...")
    engine.dispose()

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

@app.patch("/api/v1/chats/{chat_id}", response_model=ChatPublic)
async def update_chat(chat_id: uuid.UUID, data: ChatUpdate, session: SessionDep):
    chat = session.get(Chat, chat_id)
    if not chat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
    session.commit()
    session.refresh(chat)
    return chat