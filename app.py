import streamlit as st
import pandas as pd
from langchain_ollama import OllamaLLM, OllamaEmbeddings
from langchain_community.vectorstores import FAISS
from langgraph.prebuilt import create_react_agent
from langchain.tools import tool
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.documents import Document
import os
import tempfile
import whisper
from audiorecorder import audiorecorder
from pydub import AudioSegment
import time
import sqlite3
import hashlib
import json

# Grok-like CSS: Dark mode, fixed bottom input, wider chat area, auto-scroll
st.markdown("""
<style>
    /* Dark mode body and text */
    .stApp {
        background-color: #121212;
        color: #ffffff;
    }
    .stSidebar {
        background-color: #1e1e1e;
    }
    [data-testid="stSidebarNav"] {
        background-color: #1e1e1e;
    }
    /* Wider main chat area */
    [data-testid="stAppViewContainer"] > section > div {
        max-width: 90% !important;
        margin: auto;
    }
    /* Chat messages styling */
    .st-chat-message {
        background-color: #1e1e1e;
        border-radius: 10px;
        padding: 10px;
    }
    /* Fixed bottom input bar */
    .fixed-bottom-input {
        position: fixed;
        bottom: 0;
        left: 0;
        width: 100%;
        background-color: #121212;
        padding: 10px;
        z-index: 1000;
        border-top: 1px solid #333;
    }
    /* Input bar styling */
    .fixed-bottom-input [data-testid="stTextInput"] {
        height: 80px !important;
        font-size: 20px !important;
        background-color: #1e1e1e;
        color: #ffffff;
        border: 1px solid #333;
    }
    .fixed-bottom-input div.stButton > button[kind="primary"] {
        background-color: white;
        color: black;
        border: 1px solid black;
        border-radius: 50%;
        width: 40px;
        height: 40px;
        padding: 0;
        font-size: 20px;
        min-height: auto;
        display: flex;
        align-items: center;
        justify-content: center;
    }
    .fixed-bottom-input div.stButton > button[kind="primary"]:hover {
        background-color: #f0f0f0;
    }
    /* Sidebar elements light text */
    .stSidebar * {
        color: #ffffff;
    }
    .editor-button {
        width: 95% !important;
        text-align: center;
    }
    .save-button {
        border-color: green !important;
    }
    .clear-button {
        border-color: red !important;
    }
</style>
""", unsafe_allow_html=True)

# Auto-scroll JS for chat
st.markdown("""
<script>
    window.scrollTo(0, document.body.scrollHeight);
</script>
""", unsafe_allow_html=True)

# Database setup
conn = sqlite3.connect('users.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS users 
             (username TEXT PRIMARY KEY, password TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS settings 
             (username TEXT PRIMARY KEY, chat_folder TEXT)''')
conn.commit()

# Hash password
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

# Function to save chats to folder
def save_chats():
    if 'chat_folder' in st.session_state and st.session_state.chat_folder:
        for chat_name, messages in st.session_state.chats.items():
            file_path = os.path.join(st.session_state.chat_folder, f"{chat_name}.json")
            with open(file_path, 'w') as f:
                json.dump(messages, f)

# Login/Registration popup
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.username = None

if not st.session_state.logged_in:
    with st.expander("Login or Register", expanded=True):
        users = c.execute("SELECT username FROM users").fetchall()
        user_list = [u[0] for u in users]
        selected_user = st.selectbox("Select User", ["New User"] + user_list)
        password = st.text_input("Password", type="password")
        if selected_user == "New User":
            new_user = st.text_input("New Username")
            if st.button("Register"):
                if new_user and password:
                    hashed_pw = hash_password(password)
                    try:
                        c.execute("INSERT INTO users VALUES (?, ?)", (new_user, hashed_pw))
                        conn.commit()
                        st.success("User created! Log in now.")
                    except sqlite3.IntegrityError:
                        st.error("Username already exists.")
        else:
            if st.button("Login"):
                if selected_user and password:
                    hashed_pw = hash_password(password)
                    stored_pw = c.execute("SELECT password FROM users WHERE username=?", (selected_user,)).fetchone()
                    if stored_pw and stored_pw[0] == hashed_pw:
                        st.session_state.logged_in = True
                        st.session_state.username = selected_user
                        st.rerun()
                    else:
                        st.error("Invalid credentials.")

else:
    # Load user settings
    user = st.session_state.username
    chat_folder = c.execute("SELECT chat_folder FROM settings WHERE username=?", (user,)).fetchone()
    if chat_folder:
        st.session_state.chat_folder = chat_folder[0]
    else:
        st.session_state.chat_folder = ""

    # Initialize session state for logged-in user
    if "chats" not in st.session_state:
        st.session_state.chats = {"Chat 1": []}
    if "current_chat" not in st.session_state:
        st.session_state.current_chat = "Chat 1"
    if "csv_file" not in st.session_state:
        st.session_state.csv_file = None
    if "vectorstore" not in st.session_state:
        st.session_state.vectorstore = None
    if "embed_model" not in st.session_state:
        st.session_state.embed_model = "nomic-embed-text"
    if "chat_model" not in st.session_state:
        st.session_state.chat_model = "llama3.2"
    if "uploaded_files" not in st.session_state:
        st.session_state.uploaded_files = []
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = {}  # Per-chat message history for agent
    if "last_load_time" not in st.session_state:
        st.session_state.last_load_time = 0
    if "refresh_interval" not in st.session_state:
        st.session_state.refresh_interval = 0  # In minutes, 0 = no auto-refresh

    # Load user chats from folder if set
    if st.session_state.chat_folder:
        for file in os.listdir(st.session_state.chat_folder):
            if file.endswith(".json"):
                chat_name = file[:-5]
                with open(os.path.join(st.session_state.chat_folder, file), 'r') as f:
                    st.session_state.chats[chat_name] = json.load(f)

    # Function to load and process CSV for RAG with optimization and truncation
    def load_csv_for_rag(uploaded_file, chunk_size=20, max_content_length=4000):
        if uploaded_file is not None:
            df = pd.read_csv(uploaded_file)
            documents = []
            for i in range(0, len(df), chunk_size):
                chunk = df.iloc[i:i+chunk_size]
                content = " ".join(chunk.apply(lambda row: " ".join([f"{col}: {val}" for col, val in row.items() if pd.notna(val)]), axis=1))
                # Truncate to avoid context length issues
                content = content[:max_content_length]
                documents.append(Document(page_content=content))
            embeddings = OllamaEmbeddings(model=st.session_state.embed_model)  # No model_kwargs needed
            vectorstore = FAISS.from_documents(documents, embeddings)
            return vectorstore
        return None

    # Retrieval tool for agent
    @tool
    def retrieve_context(query: str) -> str:
        """Retrieve relevant context from the CSV database to answer the query."""
        if st.session_state.vectorstore:
            retrieved_docs = st.session_state.vectorstore.similarity_search(query, k=3)
            serialized = "\n\n".join(doc.page_content for doc in retrieved_docs)
            return serialized
        return "No CSV loaded."

    # Function to summarize chat for name
    def summarize_chat_name(current_chat):
        llm = OllamaLLM(model=st.session_state.chat_model)
        history = st.session_state.chat_history[current_chat]
        messages_str = '\n'.join([f"{msg.type}: {msg.content}" for msg in history])
        prompt = f"Summarize this conversation in 3-5 words: {messages_str}"
        summary = llm.invoke(prompt).content.strip()
        return summary

    # Sidebar for settings and history
    with st.sidebar:
        st.title("Settings")
        
        # Export Chat button at top left
        if st.button("Export Chat"):
            with open("chat_export.txt", "w") as f:
                for msg in st.session_state.chats[st.session_state.current_chat]:
                    f.write(f"{msg['role']}: {msg['content']}\n")
            st.download_button(
                label="Download Chat",
                data=open("chat_export.txt", "rb"),
                file_name="chat_export.txt",
                mime="text/plain"
            )

        # CSV file uploader
        csv_file = st.file_uploader("Upload CSV File", type=["csv"])
        if csv_file is not None and csv_file != st.session_state.csv_file:
            try:
                st.session_state.csv_file = csv_file
                st.session_state.vectorstore = load_csv_for_rag(csv_file)
                st.session_state.last_load_time = time.time()
                st.success("CSV loaded successfully!")
            except Exception as e:
                with st.expander("Error Details (click to expand)"):
                    st.error(str(e))

        # Refresh interval selector
        refresh_options = [0, 1, 5, 10, 30, 60]  # Minutes
        st.session_state.refresh_interval = st.selectbox("Auto-Refresh Interval (minutes)", refresh_options)

        # Dedicated embedder selector
        embed_options = ["nomic-embed-text", "llama3.2", "other-model"]
        st.session_state.embed_model = st.selectbox("Select Embedding Model", embed_options, index=embed_options.index(st.session_state.embed_model))

        # Chat model selector
        chat_options = ["llama3.2", "other-model"]
        st.session_state.chat_model = st.selectbox("Select Chat Model", chat_options, index=chat_options.index(st.session_state.chat_model))

        st.header("Chat History")
        with st.expander("Chats", expanded=True):
            for chat_name in list(st.session_state.chats.keys()):
                if st.button(chat_name):
                    st.session_state.current_chat = chat_name
            if st.button("New Chat"):
                save_chats()  # Save before new
                new_name = f"Chat {len(st.session_state.chats) + 1}"
                st.session_state.chats[new_name] = []
                st.session_state.current_chat = new_name
                st.session_state.chat_history[new_name] = []  # Initialize history for new chat

        st.header("Uploads")
        uploaded_file = st.file_uploader("Upload file/photo", type=["txt", "jpg", "png"])
        if uploaded_file:
            st.session_state.uploaded_files.append(uploaded_file)
            st.success("File uploaded!")

        # Self-editing feature
        st.header("Advanced")
        with st.expander("Edit Source Code", expanded=False):
            with open(__file__, 'r') as f:
                current_code = f.read()
            col_clear, _ = st.columns([1, 5])
            with col_clear:
                if st.button("🗑️"):
                    st.session_state.new_code = ""
                    st.rerun()
            new_code = st.text_area("Paste updated code here", value=st.session_state.get('new_code', current_code), height=400)
            if st.button("Save and Rerun"):
                with open(__file__, 'w') as f:
                    f.write(new_code)
                st.rerun()

# Check for auto-refresh
if st.session_state.refresh_interval > 0 and st.session_state.csv_file is not None:
    current_time = time.time()
    if (current_time - st.session_state.last_load_time) / 60 >= st.session_state.refresh_interval:
        try:
            st.session_state.vectorstore = load_csv_for_rag(st.session_state.csv_file)
            st.session_state.last_load_time = current_time
            st.info("CSV reloaded automatically.")
        except Exception as e:
            with st.expander("Error Details (click to expand)"):
                st.error(str(e))

# Main chat interface (no title)
current_chat = st.session_state.current_chat
chat_messages = st.session_state.chats[current_chat]

# Display chat messages
for message in chat_messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# Chat input with voice and send buttons at the bottom
col1, col2, col3 = st.columns([7, 1, 1])
with col1:
    prompt = st.text_input("Type your message...", key="user_input")
with col2:
    if st.button("🎤", key="mic_button", type="primary"):
        audio = audiorecorder("Click to record", "Recording...")
        if len(audio) > 0:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
                audio.export(tmp_file.name, format="wav")
                model = whisper.load_model("base")
                result = model.transcribe(tmp_file.name)
                prompt = result["text"]
                os.remove(tmp_file.name)
            # Simulate send after voice transcription
            st.session_state.prompt = prompt
            st.rerun()
with col3:
    if st.button("Send", key="send_button"):
        st.session_state.prompt = prompt
        st.rerun()

if 'prompt' in st.session_state and st.session_state.prompt:
    prompt = st.session_state.prompt
    del st.session_state.prompt  # Clear after use
    with st.chat_message("user"):
        st.markdown(prompt)
    chat_messages.append({"role": "user", "content": prompt})

    try:
        # Agent setup if CSV loaded
        if st.session_state.vectorstore:
            llm = OllamaLLM(model=st.session_state.chat_model)  # Separate chat model
            
            # String system prompt for structured output
            system_prompt = (
                "You are a helpful assistant. Use the retrieval tool to get context from the CSV database if needed to answer the query. "
                "For queries involving orders or data from CSVs, structure your response in a clean Markdown table with columns like Order ID, Date, Details, Status."
            )
            
            # Create agent with retrieval tool
            tools = [retrieve_context]
            agent = create_react_agent(llm, tools, messages_modifier=system_prompt)
            
            # Prepare message history for current chat
            if current_chat not in st.session_state.chat_history:
                st.session_state.chat_history[current_chat] = []
            
            messages = st.session_state.chat_history[current_chat] + [HumanMessage(content=prompt)]
            
            # Invoke agent
            response = agent.invoke({"messages": messages})["messages"][-1].content
            
            # Update history
            st.session_state.chat_history[current_chat] = messages + [AIMessage(content=response)]
        else:
            response = "No CSV loaded. Please upload a CSV file in settings."

        with st.chat_message("assistant"):
            st.markdown(response)
        chat_messages.append({"role": "assistant", "content": response})

        # Auto-summarize chat name if applicable
        if current_chat.startswith("Chat ") and len(st.session_state.chat_history[current_chat]) >= 3:
            summary = summarize_chat_name(current_chat)
            new_name = summary
            # Rename chat
            st.session_state.chats[new_name] = st.session_state.chats.pop(current_chat)
            st.session_state.chat_history[new_name] = st.session_state.chat_history.pop(current_chat)
            st.session_state.current_chat = new_name
            st.rerun()
    except Exception as e:
        with st.expander("Error Details (click to expand)"):
            st.error(str(e))

# Save chats on app close or change
save_chats()