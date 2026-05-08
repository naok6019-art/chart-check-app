from datetime import date
import hashlib
from io import BytesIO
import re

import pandas as pd
import streamlit as st

try:
    import authlib  # noqa: F401
except ImportError:
    authlib = None

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    gspread = None
    Credentials = None

APP_VERSION = "2026-05-09-user-login-sheets-v2"
DATA_FILE = "karte_checklist.csv"
WORKSHEET_PREFIX = "karte_"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
CHECK_ITEMS = [
    "指示書の確認",
    "署名付き計画書の確認",
    "報告書の確認",
    "提供票の確認",
    "介護保険証・負担割合証／医療保険証の確認",
    "実績入力の確認",
]
COLUMNS = ["対象月", "利用者名"] + CHECK_ITEMS


def has_secret(key: str) -> bool:
    try:
        return key in st.secrets
    except Exception:
        return False


def has_google_settings() -> bool:
    return has_secret("spreadsheet_id") and has_secret("gcp_service_account")


def has_auth_settings() -> bool:
    try:
        auth = st.secrets.get("auth", {})
        required = [
            "redirect_uri",
            "cookie_secret",
            "client_id",
            "client_secret",
            "server_metadata_url",
        ]
        return all(key in auth and str(auth[key]).strip() for key in required)
    except Exception:
        return False


def is_logged_in() -> bool:
    try:
        return bool(st.user.is_logged_in)
    except Exception:
        return False


def current_email() -> str:
    try:
        return str(st.user.email).strip().lower()
    except Exception:
        return ""


def worksheet_name_for_email(email: str) -> str:
    normalized = email.strip().lower()
    safe = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_") or "user"
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{WORKSHEET_PREFIX}{safe}_{digest}"[:100]


def normalize_text(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def normalize_bool(value) -> bool:
    if value is True:
        return True
    if value is False or pd.isna(value):
        return False
    return str(value).strip().lower() in ["true", "1", "yes", "y", "済", "確認済み"]


def normalize_data(data: pd.DataFrame) -> pd.DataFrame:
    for column in COLUMNS:
        if column not in data.columns:
            data[column] = False if column in CHECK_ITEMS else ""
    for column in ["対象月", "利用者名"]:
        data[column] = data[column].map(normalize_text)
    for item in CHECK_ITEMS:
        data[item] = data[item].map(normalize_bool)
    return data[COLUMNS]


@st.cache_resource
def get_worksheet(worksheet_name: str):
    if gspread is None or Credentials is None:
        raise RuntimeError("Googleスプレッドシート用ライブラリがありません。requirements.txtを確認してください。")
    credentials = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=SCOPES,
    )
    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(st.secrets["spreadsheet_id"])
    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=len(COLUMNS))
        worksheet.update([COLUMNS])
    return worksheet


def load_data() -> pd.DataFrame:
    if has_google_settings():
        worksheet = get_worksheet(worksheet_name_for_email(current_email()))
        return normalize_data(pd.DataFrame(worksheet.get_all_records()))
    try:
        return normalize_data(pd.read_csv(DATA_FILE, encoding="utf-8-sig"))
    except FileNotFoundError:
        return pd.DataFrame(columns=COLUMNS)


def save_data(data: pd.DataFrame) -> None:
    if has_google_settings():
        worksheet = get_worksheet(worksheet_name_for_email(current_email()))
        sheet_data = data[COLUMNS].copy()
        for item in CHECK_ITEMS:
            sheet_data[item] = sheet_data[item].map(lambda value: "TRUE" if bool(value) else "FALSE")
        worksheet.clear()
        worksheet.update([COLUMNS] + sheet_data.values.tolist())
    else:
        data[COLUMNS].to_csv(DATA_FILE, index=False, encoding="utf-8-sig")


def user_status(row: pd.Series) -> str:
    checked = sum(bool(row[item]) for item in CHECK_ITEMS)
    if checked == len(CHECK_ITEMS):
        return "完了"
    if checked == 0:
        return "未着手"
    return "確認中"


def add_display_columns(data: pd.DataFrame) -> pd.DataFrame:
    display = data.copy()
    display["状況"] = display.apply(user_status, axis=1)
    display["未確認項目"] = display.apply(
        lambda row: "、".join(item for item in CHECK_ITEMS if not bool(row[item])),
        axis=1,
    )
    return display


def read_user_list(uploaded_file) -> pd.DataFrame:
    file_bytes = uploaded_file.getvalue()
    try:
        users = pd.read_csv(BytesIO(file_bytes), encoding="utf-8-sig")
    except UnicodeDecodeError:
        users = pd.read_csv(BytesIO(file_bytes), encoding="cp932")
    if "利用者名" not in users.columns:
        raise ValueError("CSVに「利用者名」列が必要です。")
    users = users[["利用者名"]].copy()
    users["利用者名"] = users["利用者名"].map(normalize_text)
    return users[users["利用者名"] != ""].drop_duplicates()


def create_monthly_records(data: pd.DataFrame, target_month: str, users: pd.DataFrame):
    records = []
    skipped = 0
    for _, row in users.iterrows():
        user_name = row["利用者名"]
        exists = ((data["対象月"] == target_month) & (data["利用者名"] == user_name)).any()
        if exists:
            skipped += 1
            continue
        record = {"対象月": target_month, "利用者名": user_name}
        for item in CHECK_ITEMS:
            record[item] = False
        records.append(record)
    if records:
        data = pd.concat([data, pd.DataFrame(records)], ignore_index=True)
    return data, len(records), skipped


def require_login() -> None:
    if not has_google_settings():
        return
    if authlib is None:
        st.error("Googleログインに必要なAuthlibがありません。Streamlit Cloudで再デプロイしてください。")
        st.stop()
    if not has_auth_settings():
        st.error("Googleログイン設定が不足しています。Streamlit CloudのSecretsに[auth]設定を追加してください。")
        st.stop()
    if not is_logged_in():
        st.info("このアプリを使うにはGoogleログインが必要です。")
        if st.button("Googleでログイン"):
            st.login()
        st.stop()


st.set_page_config(page_title="月次カルテ確認チェックリスト", layout="wide")
st.title("訪問看護ステーション 月次カルテ確認チェックリスト")
st.caption("ローカルではCSV、WebアプリではGoogleスプレッドシートに保存できます。")
st.caption(f"アプリ版: {APP_VERSION}")

require_login()

data = load_data()

if has_google_settings():
    st.success("保存先: Googleスプレッドシート")
    st.caption(f"ログイン中: {current_email()}")
    st.caption(f"保存シート: {worksheet_name_for_email(current_email())}")
    if st.button("ログアウト"):
        st.logout()
else:
    st.info("保存先: ローカルCSV")

st.header("月次データの作成")
default_month = date.today().strftime("%Y-%m")

with st.expander("利用者一覧CSVからまとめて作成", expanded=True):
    import_month = st.text_input("作成する対象月", value=default_month, help="例: 2026-05")
    uploaded_file = st.file_uploader("利用者一覧CSVを選択", type=["csv"], help="CSVには「利用者名」の列を入れてください。")
    if uploaded_file is not None:
        try:
            user_list = read_user_list(uploaded_file)
            st.dataframe(user_list, use_container_width=True, hide_index=True)
            if st.button("この利用者一覧で月次データを作成"):
                data, added, skipped = create_monthly_records(data, import_month.strip(), user_list)
                save_data(data)
                st.success(f"{added}件を追加しました。登録済みのため{skipped}件はスキップしました。")
                st.rerun()
        except ValueError as error:
            st.error(str(error))

with st.expander("1人ずつ手入力で追加"):
    with st.form("single_record_form"):
        col1, col2 = st.columns(2)
        target_month = col1.text_input("対象月", value=default_month, key="single_month")
        user_name = col2.text_input("利用者名", placeholder="例: 佐藤 太郎")
        if st.form_submit_button("未確認として追加"):
            target_month = target_month.strip()
            user_name = user_name.strip()
            if not target_month or not user_name:
                st.error("対象月、利用者名を入力してください。")
            elif ((data["対象月"] == target_month) & (data["利用者名"] == user_name)).any():
                st.warning("同じ対象月・利用者名の登録がすでにあります。")
            else:
                record = {"対象月": target_month, "利用者名": user_name}
                for item in CHECK_ITEMS:
                    record[item] = False
                data = pd.concat([data, pd.DataFrame([record])], ignore_index=True)
                save_data(data)
                st.success("未確認データとして追加しました。")
                st.rerun()

st.header("絞り込み")
if data.empty:
    st.info("まだデータがありません。月次データを作成してください。")
    filtered = data.copy()
else:
    month_options = ["すべて"] + sorted(data["対象月"].dropna().unique(), reverse=True)
    selected_month = st.selectbox("対象月で絞り込み", month_options)
    filtered = data.copy() if selected_month == "すべて" else data[data["対象月"] == selected_month].copy()
    st.caption(f"表示中: {len(filtered)}件 / 全体: {len(data)}件")

st.header("チェック結果の更新保存")
if filtered.empty:
    st.info("更新できるデータがありません。")
else:
    editor = filtered.copy()
    editor.insert(0, "保存用ID", editor.index)
    editor = add_display_columns(editor)
    edited = st.data_editor(
        editor[["保存用ID", "対象月", "利用者名"] + CHECK_ITEMS + ["状況", "未確認項目"]],
        use_container_width=True,
        hide_index=True,
        disabled=["保存用ID", "対象月", "利用者名", "状況", "未確認項目"],
    )
    if st.button("編集したチェック結果を保存"):
        for _, row in edited.iterrows():
            original_index = int(row["保存用ID"])
            for item in CHECK_ITEMS:
                data.at[original_index, item] = bool(row[item])
        save_data(data)
        st.success("チェック結果を保存しました。")
        st.rerun()

st.header("進捗確認")
if filtered.empty:
    st.info("進捗を表示できるデータがありません。")
else:
    display = add_display_columns(filtered)
    total_users = len(filtered)
    completed = int((display["状況"] == "完了").sum())
    checked_items = int(filtered[CHECK_ITEMS].sum().sum())
    total_items = total_users * len(CHECK_ITEMS)
    rate = checked_items / total_items if total_items else 0
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("利用者数", total_users)
    col2.metric("完了", completed)
    col3.metric("未完了", total_users - completed)
    col4.metric("進捗率", f"{rate:.0%}")

st.header("月末の未完了一覧")
if data.empty:
    st.info("未完了一覧を表示できるデータがありません。")
else:
    report_month = st.selectbox("月末確認する対象月", sorted(data["対象月"].dropna().unique(), reverse=True))
    month_data = data[data["対象月"] == report_month].copy()
    incomplete = month_data[month_data[CHECK_ITEMS].eq(False).any(axis=1)].copy()
    if incomplete.empty:
        st.success(f"{report_month} はすべて確認完了です。")
    else:
        incomplete_display = add_display_columns(incomplete)
        st.warning(f"{report_month} の未完了は {len(incomplete_display)}件です。")
        st.dataframe(incomplete_display[["対象月", "利用者名", "未確認項目", "状況"]], use_container_width=True, hide_index=True)
        st.download_button(
            "月末未完了一覧CSVをダウンロード",
            incomplete_display.to_csv(index=False, encoding="utf-8-sig"),
            file_name=f"{report_month}_未完了一覧.csv",
            mime="text/csv",
        )

st.header("一覧表示とCSV出力")
if filtered.empty:
    st.info("表示できるデータがありません。")
else:
    display = add_display_columns(filtered)
    st.dataframe(display, use_container_width=True, hide_index=True)
    st.download_button(
        "表示中の一覧CSVをダウンロード",
        display.to_csv(index=False, encoding="utf-8-sig"),
        file_name="karte_checklist_filtered.csv",
        mime="text/csv",
    )

st.header("登録データの削除")
if data.empty:
    st.info("削除できる登録データはありません。")
else:
    delete_options = {index: f'{row["対象月"]} / {row["利用者名"]}' for index, row in data.iterrows()}
    selected_index = st.selectbox("削除する登録を選択", list(delete_options.keys()), format_func=lambda index: delete_options[index])
    confirm_delete = st.checkbox("選択した登録データを削除することを確認しました")
    if st.button("選択した登録を削除", disabled=not confirm_delete):
        data = data.drop(index=selected_index).reset_index(drop=True)
        save_data(data)
        st.success("選択した登録データを削除しました。")
        st.rerun()
