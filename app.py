import time
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import firebase_admin
from firebase_admin import credentials, firestore
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from urllib.parse import urlparse


st.set_page_config(
    page_title="YouTube Channel Video Search",
    page_icon="🔎",
    layout="wide"
)

import os
import json

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
FIRESTORE_DATABASE_ID = os.environ.get("FIRESTORE_DATABASE_ID", "youtube-search-tool")

if not YOUTUBE_API_KEY:
    st.error("Thiếu YOUTUBE_API_KEY trong Environment Variables.")
    st.stop()

if not FIREBASE_SERVICE_ACCOUNT_JSON:
    st.error("Thiếu FIREBASE_SERVICE_ACCOUNT_JSON trong Environment Variables.")
    st.stop()


@st.cache_resource
def init_firebase():
    if not firebase_admin._apps:
        service_account_info = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
        cred = credentials.Certificate(service_account_info)
        firebase_admin.initialize_app(cred)

    return firestore.client(database_id=FIRESTORE_DATABASE_ID)


@st.cache_resource
def init_youtube():
    return build(
        "youtube",
        "v3",
        developerKey=YOUTUBE_API_KEY,
        cache_discovery=False
    )


db = init_firebase()
youtube = init_youtube()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def extract_channel_hint(channel_url):
    if not channel_url:
        return None, None

    channel_url = channel_url.strip()
    parsed = urlparse(channel_url)
    path = parsed.path.strip("/")
    parts = path.split("/")

    if not parts or parts[0] == "":
        return None, None

    if parts[0] == "channel" and len(parts) >= 2:
        return "id", parts[1]

    if parts[0].startswith("@"):
        return "handle", parts[0]

    if parts[0] == "user" and len(parts) >= 2:
        return "username", parts[1]

    if parts[0] == "c" and len(parts) >= 2:
        return "search", parts[1]

    return "search", parts[0]


def resolve_channel(channel_url):
    hint_type, value = extract_channel_hint(channel_url)

    if not hint_type or not value:
        raise ValueError("Link YouTube không hợp lệ.")

    request_kwargs = {
        "part": "snippet,contentDetails",
        "maxResults": 1,
    }

    if hint_type == "id":
        request_kwargs["id"] = value

    elif hint_type == "handle":
        request_kwargs["forHandle"] = value

    elif hint_type == "username":
        request_kwargs["forUsername"] = value

    elif hint_type == "search":
        search_resp = youtube.search().list(
            part="snippet",
            q=value,
            type="channel",
            maxResults=1
        ).execute()

        items = search_resp.get("items", [])
        if not items:
            raise ValueError("Không tìm thấy kênh từ link này.")

        channel_id = items[0]["snippet"]["channelId"]
        request_kwargs["id"] = channel_id

    response = youtube.channels().list(**request_kwargs).execute()
    items = response.get("items", [])

    if not items:
        raise ValueError("Không tìm thấy kênh YouTube.")

    item = items[0]
    channel_id = item["id"]
    channel_title = item["snippet"]["title"]
    uploads_playlist_id = item["contentDetails"]["relatedPlaylists"]["uploads"]

    return {
        "channel_id": channel_id,
        "channel_title": channel_title,
        "uploads_playlist_id": uploads_playlist_id,
        "link": channel_url,
    }


def get_all_imported_channels():
    docs = db.collection("channels").stream()
    channels = []

    for doc in docs:
        data = doc.to_dict()
        data["_doc_id"] = doc.id
        channels.append(data)

    return channels


def channel_exists(channel_id):
    doc = db.collection("channels").document(channel_id).get()
    return doc.exists


def save_channel(channel_data):
    channel_id = channel_data["channel_id"]
    payload = {
        **channel_data,
        "imported_at": now_iso(),
    }
    db.collection("channels").document(channel_id).set(payload)


def delete_channel_and_videos(channel_id):
    db.collection("channels").document(channel_id).delete()

    videos = db.collection("videos").where("channel_id", "==", channel_id).stream()
    batch = db.batch()
    count = 0

    for video in videos:
        batch.delete(video.reference)
        count += 1

        if count % 400 == 0:
            batch.commit()
            batch = db.batch()

    batch.commit()
    return count


def fetch_video_ids_from_uploads_playlist(uploads_playlist_id):
    video_ids = []
    next_page_token = None

    while True:
        resp = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=next_page_token
        ).execute()

        for item in resp.get("items", []):
            video_id = item["contentDetails"].get("videoId")
            if video_id:
                video_ids.append(video_id)

        next_page_token = resp.get("nextPageToken")

        if not next_page_token:
            break

    return video_ids


def chunks(items, size=50):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fetch_video_details(video_ids, channel_data):
    all_videos = []

    for batch_ids in chunks(video_ids, 50):
        resp = youtube.videos().list(
            part="snippet,statistics",
            id=",".join(batch_ids),
            maxResults=50
        ).execute()

        for item in resp.get("items", []):
            video_id = item["id"]
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})

            video_data = {
                "video_id": video_id,
                "video_url": f"https://www.youtube.com/watch?v={video_id}",
                "channel_id": channel_data["channel_id"],
                "channel_title": channel_data["channel_title"],
                "title": snippet.get("title", ""),
                "view_count": safe_int(stats.get("viewCount", 0)),
                "published_at": snippet.get("publishedAt", ""),
                "fetched_at": now_iso(),
            }

            all_videos.append(video_data)

    return all_videos


def save_videos_to_firestore(videos):
    batch = db.batch()
    count = 0

    for video in videos:
        ref = db.collection("videos").document(video["video_id"])
        batch.set(ref, video)
        count += 1

        if count % 400 == 0:
            batch.commit()
            batch = db.batch()

    batch.commit()
    return count


def refresh_all_videos():
    channels = get_all_imported_channels()
    total_videos = 0
    errors = []

    for channel in channels:
        try:
            video_ids = fetch_video_ids_from_uploads_playlist(
                channel["uploads_playlist_id"]
            )
            videos = fetch_video_details(video_ids, channel)
            saved_count = save_videos_to_firestore(videos)
            total_videos += saved_count

        except Exception as e:
            errors.append({
                "channel": channel.get("channel_title", channel.get("link", "")),
                "error": str(e),
            })

    return total_videos, errors


def get_all_videos():
    docs = db.collection("videos").stream()
    videos = []

    for doc in docs:
        data = doc.to_dict()
        videos.append(data)

    return videos


def prepare_video_dataframe(videos):
    if not videos:
        return pd.DataFrame(columns=[
            "Link video", "Số view", "Tiêu đề", "Kênh", "Ngày đăng"
        ])

    rows = []

    for v in videos:
        rows.append({
            "Link video": v.get("video_url", ""),
            "Số view": safe_int(v.get("view_count", 0)),
            "Tiêu đề": v.get("title", ""),
            "Kênh": v.get("channel_title", ""),
            "Ngày đăng": v.get("published_at", ""),
        })

    df = pd.DataFrame(rows)

    if not df.empty:
        df["Ngày đăng datetime"] = pd.to_datetime(
            df["Ngày đăng"],
            errors="coerce",
            utc=True
        )

    return df


def filter_and_sort_videos(df, sort_mode, keyword):
    if df.empty:
        return df

    result = df.copy()

    if keyword:
        keyword = keyword.strip()
        if keyword:
            result = result[
                result["Tiêu đề"].str.contains(keyword, case=False, na=False)
            ]

    now = pd.Timestamp.now(tz="UTC")

    if sort_mode == "view":
        result = result.sort_values("Số view", ascending=False)

    elif sort_mode == "mới nhất":
        result = result.sort_values("Ngày đăng datetime", ascending=False)

    elif sort_mode == "tuần này":
        seven_days_ago = now - pd.Timedelta(days=7)
        result = result[result["Ngày đăng datetime"] >= seven_days_ago]
        result = result.sort_values("Số view", ascending=False)

    elif sort_mode == "tháng":
        thirty_days_ago = now - pd.Timedelta(days=30)
        result = result[result["Ngày đăng datetime"] >= thirty_days_ago]
        result = result.sort_values("Số view", ascending=False)

    result = result.drop(columns=["Ngày đăng datetime"], errors="ignore")
    return result


st.title("YouTube Channel Video Search")

tab_import, tab_search = st.tabs(["Import", "Search"])


with tab_import:
    st.subheader("Import kênh YouTube")

    if "import_status" not in st.session_state:
        st.session_state.import_status = ""

    col_input, col_button = st.columns([5, 1])

    with col_input:
        channel_link = st.text_input(
            "Nhập link kênh YouTube",
            placeholder="Ví dụ: https://www.youtube.com/@tenkenh"
        )

    with col_button:
        st.write("")
        st.write("")
        import_clicked = st.button("Import", use_container_width=True)

    if import_clicked:
        try:
            channel_data = resolve_channel(channel_link)

            if channel_exists(channel_data["channel_id"]):
                st.session_state.import_status = "Đã có"
            else:
                save_channel(channel_data)
                st.session_state.import_status = "Import thành công"

        except HttpError as e:
            st.session_state.import_status = f"Lỗi: {e}"
        except Exception as e:
            st.session_state.import_status = f"Lỗi: {e}"

    if st.session_state.import_status:
        if st.session_state.import_status == "Đã có":
            st.warning("Đã có")
        elif st.session_state.import_status.startswith("Lỗi"):
            st.error(st.session_state.import_status)
        else:
            st.success(st.session_state.import_status)

    st.divider()
    st.subheader("List danh sách kênh đã import")

    channels = get_all_imported_channels()

    if not channels:
        st.info("Chưa có kênh nào được import.")
    else:
        header_cols = st.columns([4, 3, 1])
        header_cols[0].markdown("**Link**")
        header_cols[1].markdown("**Tên kênh**")
        header_cols[2].markdown("**Xóa**")

        for channel in channels:
            row_cols = st.columns([4, 3, 1])

            row_cols[0].write(channel.get("link", ""))
            row_cols[1].write(channel.get("channel_title", ""))

            delete_key = f"delete_{channel['channel_id']}"

            if row_cols[2].button("Xóa", key=delete_key):
                deleted_videos = delete_channel_and_videos(channel["channel_id"])
                st.success(
                    f"Đã xóa kênh và {deleted_videos} video liên quan."
                )
                time.sleep(1)
                st.rerun()


with tab_search:
    st.subheader("Tìm kiếm video từ các kênh đã import")

    if "search_keyword" not in st.session_state:
        st.session_state.search_keyword = ""

    if "sort_mode" not in st.session_state:
        st.session_state.sort_mode = "view"

    top_cols = st.columns([1, 3])

    with top_cols[0]:
        refresh_clicked = st.button("Refresh", use_container_width=True)

    with top_cols[1]:
        st.caption(
            "Refresh sẽ lấy lại toàn bộ video từ các kênh đã import và cập nhật vào Firebase."
        )

    if refresh_clicked:
        with st.spinner("Đang làm mới dữ liệu video từ YouTube..."):
            try:
                total_videos, errors = refresh_all_videos()
                st.success(f"Đã cập nhật {total_videos} video vào Firebase.")

                if errors:
                    st.warning("Một số kênh bị lỗi khi refresh:")
                    st.json(errors)

            except Exception as e:
                st.error(f"Lỗi khi Refresh: {e}")

    st.divider()

    control_cols = st.columns([2, 4, 1])

    with control_cols[0]:
        sort_mode = st.selectbox(
            "Sắp xếp",
            ["view", "mới nhất", "tuần này", "tháng"],
            index=["view", "mới nhất", "tuần này", "tháng"].index(
                st.session_state.sort_mode
            )
        )
        st.session_state.sort_mode = sort_mode

    with control_cols[1]:
        keyword_input = st.text_input(
            "Ô tìm kiếm",
            value=st.session_state.search_keyword,
            placeholder="Ví dụ: Shoplifter"
        )

    with control_cols[2]:
        st.write("")
        st.write("")
        filter_clicked = st.button("Lọc", use_container_width=True)

    if filter_clicked:
        st.session_state.search_keyword = keyword_input

    videos = get_all_videos()
    df = prepare_video_dataframe(videos)

    filtered_df = filter_and_sort_videos(
        df,
        st.session_state.sort_mode,
        st.session_state.search_keyword
    )

    st.write(f"Tổng số video hiển thị: **{len(filtered_df)}**")

    if filtered_df.empty:
        st.info("Chưa có dữ liệu video hoặc không có video phù hợp.")
    else:
        st.dataframe(
            filtered_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Link video": st.column_config.LinkColumn("Link video"),
                "Số view": st.column_config.NumberColumn(
                    "Số view",
                    format="%d"
                ),
            }
        )
