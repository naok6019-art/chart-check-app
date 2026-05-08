from datetime import date
import hashlib
from io import BytesIO
from pathlib import Path
import re

import pandas as pd
import streamlit as st

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    gspread = None
    Credentials = None


# ==============================
# 基本設定
# ==============================

# チェック結果を保存するCSVファイルです。
# このCSVが「簡易データベース」の役割になります。
# アプリを閉じたり、PCを再起動したりしても、このCSVが残っていればデータも残ります。
DATA_FILE = Path("karte_checklist.csv")

# Googleスプレッドシートに保存する場合のシート名の先頭に付ける文字です。
# 実際には、ログインしたメールアドレスごとに別シートを作ります。
GOOGLE_SHEET_WORKSHEET_PREFIX = "karte_"

# Googleスプレッドシートにアクセスするための権限範囲です。
# 読み書きするために spreadsheets、共有設定を確認するために drive を使います。
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# 利用者一覧CSVを取り込むときに必要な列です。
# 1人で管理する前提なので、看護師名は使わず「利用者名」だけで管理します。
USER_LIST_COLUMNS = ["利用者名"]

# 月次で確認する項目です。
# 項目を増やしたい場合は、このリストに追加してください。
CHECK_ITEMS = [
    "指示書の確認",
    "署名付き計画書の確認",
    "報告書の確認",
    "提供票の確認",
    "介護保険証・負担割合証／医療保険証の確認",
    "実績入力の確認",
]

# 保存CSVに持たせる列です。
# ここにある列だけを保存することで、画面表示用の列がCSVに混ざらないようにします。
COLUMNS = ["対象月", "利用者名"] + CHECK_ITEMS

# Web公開後に、Streamlit Cloudへ最新コードが反映されているか確認するための表示です。
APP_VERSION = "2026-05-08-user-login-sheets"


# ==============================
# データ処理
# ==============================

def has_google_sheets_settings() -> bool:
    """Googleスプレッドシート保存に必要な設定があるか確認します。"""
    try:
        return (
            "spreadsheet_id" in st.secrets
            and "gcp_service_account" in st.secrets
        )
    except Exception:
        # ローカル利用では secrets.toml を作らないことが多いです。
        # その場合はGoogleスプレッドシート保存を使わず、CSV保存に切り替えます。
        return False


def has_auth_settings() -> bool:
    """Googleログインに必要なStreamlit認証設定があるか確認します。"""
    try:
        auth_settings = st.secrets.get("auth", {})
        return all(
            key in auth_settings
            for key in ["redirect_uri", "cookie_secret", "client_id", "client_secret", "server_metadata_url"]
        )
    except Exception:
        return False


def is_user_logged_in() -> bool:
    """現在の利用者がログイン済みか確認します。"""
    try:
        return bool(st.user.is_logged_in)
    except Exception:
        return False


def get_current_user_email() -> str:
    """ログイン中ユーザーのメールアドレスを取得します。"""
    try:
        return str(st.user.email).strip().lower()
    except Exception:
        return ""


def create_user_worksheet_name(email: str) -> str:
    """メールアドレスから、Googleスプレッドシートのシート名を作ります。"""
    normalized_email = email.strip().lower()

    # Googleスプレッドシートのシート名に使いにくい文字を _ に置き換えます。
    safe_email = re.sub(r"[^a-z0-9]+", "_", normalized_email)
    safe_email = safe_email.strip("_") or "user"

    # シート名が長くなりすぎないように、末尾に短い識別子を付けます。
    digest = hashlib.sha1(normalized_email.encode("utf-8")).hexdigest()[:8]
    worksheet_name = f"{GOOGLE_SHEET_WORKSHEET_PREFIX}{safe_email}_{digest}"

    return worksheet_name[:100]


def get_storage_label() -> str:
    """現在どこに保存する設定か、画面に表示する文字を返します。"""
    if has_google_sheets_settings():
        return "Googleスプレッドシート"
    return "ローカルCSV"


def show_storage_status() -> None:
    """保存先の状態と、設定方法の案内を表示します。"""
    if has_google_sheets_settings():
        st.success("保存先: Googleスプレッドシート")
        user_email = get_current_user_email()
        if user_email:
            st.caption(f"ログイン中: {user_email}")
            st.caption(f"保存シート: {create_user_worksheet_name(user_email)}")
        return

    st.info("保存先: ローカルCSV")

    with st.expander("保存先について"):
        st.write(
            "この表示のままで問題ありません。"
            "今の状態では、入力データはこのアプリのフォルダ内にある "
            "`karte_checklist.csv` に保存されます。"
        )
        st.write(
            "ブラウザ画面からGoogleスプレッドシート保存へ切り替えることはできません。"
            "Webアプリ化するときだけ、Streamlit Community CloudのSecretsに"
            "Googleスプレッドシート設定を登録します。"
        )
        st.write(
            "まずローカルPCで使う場合は、このまま使って大丈夫です。"
        )


def require_login_for_google_sheets() -> None:
    """Googleスプレッドシート保存時はGoogleログインを必須にします。"""
    if not has_google_sheets_settings():
        return

    if not has_auth_settings():
        st.error("Googleログイン設定がまだ入っていません。")
        st.write(
            "ユーザーごとに別データで使うには、Streamlit CloudのSecretsに "
            "`[auth]` 設定を追加してください。"
        )
        st.stop()

    if not is_user_logged_in():
        st.info("このアプリを使うにはGoogleログインが必要です。")
        if st.button("Googleでログイン"):
            st.login()
        st.stop()


@st.cache_resource
def get_google_worksheet(worksheet_name: str):
    """Googleスプレッドシートのワークシートを取得します。"""
    if gspread is None or Credentials is None:
        raise RuntimeError(
            "Googleスプレッドシート保存に必要なライブラリがありません。"
            "requirements.txt の内容をインストールしてください。"
        )

    credentials = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=GOOGLE_SCOPES,
    )
    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(st.secrets["spreadsheet_id"])

    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=worksheet_name,
            rows=1000,
            cols=len(COLUMNS),
        )
        worksheet.update([COLUMNS])

    return worksheet

def normalize_text(value) -> str:
    """CSVや入力欄から来た値を、扱いやすい文字に整えます。"""
    if pd.isna(value):
        return ""
    return str(value).strip()


def normalize_bool(value) -> bool:
    """CSV内の True / False や 1 / 0 を、Pythonの真偽値にそろえます。"""
    if value is True:
        return True
    if value is False or pd.isna(value):
        return False

    text = str(value).strip().lower()
    return text in ["true", "1", "yes", "y", "済", "確認済み"]


def load_data() -> pd.DataFrame:
    """保存済みデータを読み込みます。なければ空の表を作ります。"""
    if has_google_sheets_settings():
        worksheet = get_google_worksheet(create_user_worksheet_name(get_current_user_email()))
        records = worksheet.get_all_records()
        data = pd.DataFrame(records)
        return normalize_data(data)

    return load_csv_data()


def load_csv_data() -> pd.DataFrame:
    """ローカルCSVからデータを読み込みます。"""
    if DATA_FILE.exists():
        data = pd.read_csv(DATA_FILE, encoding="utf-8-sig")
    else:
        data = pd.DataFrame(columns=COLUMNS)

    return normalize_data(data)


def normalize_data(data: pd.DataFrame) -> pd.DataFrame:
    """読み込んだデータの列と値を、アプリで扱いやすい形に整えます。"""
    # 以前の版で保存したCSVに「看護師名」列が残っていても問題ありません。
    # この版では使わないため、保存時には自動的に除外されます。

    # 古いCSVや手で編集したCSVでも動くように、不足列を補います。
    for column in COLUMNS:
        if column not in data.columns:
            data[column] = False if column in CHECK_ITEMS else ""

    # 文字列の列は前後の空白を取り除きます。
    for column in ["対象月", "利用者名"]:
        data[column] = data[column].map(normalize_text)

    # チェック項目は必ず True / False にそろえます。
    for item in CHECK_ITEMS:
        data[item] = data[item].map(normalize_bool)

    return data[COLUMNS]


def save_data(data: pd.DataFrame) -> None:
    """現在のチェック結果を保存します。"""
    if has_google_sheets_settings():
        save_google_sheets_data(data)
    else:
        save_csv_data(data)


def save_csv_data(data: pd.DataFrame) -> None:
    """現在のチェック結果をローカルCSVに保存します。"""
    data[COLUMNS].to_csv(DATA_FILE, index=False, encoding="utf-8-sig")


def save_google_sheets_data(data: pd.DataFrame) -> None:
    """現在のチェック結果をGoogleスプレッドシートに保存します。"""
    worksheet = get_google_worksheet(create_user_worksheet_name(get_current_user_email()))
    save_data_for_sheet = data[COLUMNS].copy()

    # Googleスプレッドシートには True / False を文字として保存します。
    # こうしておくと、次に読み込むときも判定が安定します。
    for item in CHECK_ITEMS:
        save_data_for_sheet[item] = save_data_for_sheet[item].map(
            lambda value: "TRUE" if bool(value) else "FALSE"
        )

    rows = [COLUMNS] + save_data_for_sheet.values.tolist()

    worksheet.clear()
    worksheet.update(rows)


def calculate_user_status(row: pd.Series) -> str:
    """利用者ごとの確認状況を返します。"""
    checked_count = sum(bool(row[item]) for item in CHECK_ITEMS)

    if checked_count == len(CHECK_ITEMS):
        return "完了"
    if checked_count == 0:
        return "未着手"
    return "確認中"


def add_status_columns(data: pd.DataFrame) -> pd.DataFrame:
    """画面表示用に「状況」と「未確認項目」を追加します。"""
    display_data = data.copy()
    display_data["状況"] = display_data.apply(calculate_user_status, axis=1)
    display_data["未確認項目"] = display_data.apply(
        lambda row: "、".join(item for item in CHECK_ITEMS if not bool(row[item])),
        axis=1,
    )
    return display_data


def filter_data(data: pd.DataFrame, target_month: str) -> pd.DataFrame:
    """対象月で一覧を絞り込みます。"""
    if target_month == "すべて":
        return data.copy()

    return data[data["対象月"] == target_month].copy()


def highlight_incomplete_rows(row: pd.Series) -> list[str]:
    """未確認項目がある行に薄い黄色を付けます。"""
    has_unchecked_item = any(not bool(row[item]) for item in CHECK_ITEMS)

    if has_unchecked_item:
        return ["background-color: #fff3cd"] * len(row)

    return [""] * len(row)


def read_uploaded_user_list(uploaded_file) -> pd.DataFrame:
    """アップロードされた利用者一覧CSVを読み込みます。"""
    file_bytes = uploaded_file.getvalue()

    # Excelで作ったCSVは cp932 のことがあります。
    # まずUTF-8、それで読めなければcp932で読み直します。
    try:
        user_list = pd.read_csv(BytesIO(file_bytes), encoding="utf-8-sig")
    except UnicodeDecodeError:
        user_list = pd.read_csv(BytesIO(file_bytes), encoding="cp932")

    missing_columns = [column for column in USER_LIST_COLUMNS if column not in user_list.columns]
    if missing_columns:
        raise ValueError(f"CSVに必要な列がありません: {', '.join(missing_columns)}")

    user_list = user_list[USER_LIST_COLUMNS].copy()
    user_list["利用者名"] = user_list["利用者名"].map(normalize_text)

    # 空欄行や同じ利用者の重複行を取り除きます。
    user_list = user_list[user_list["利用者名"] != ""]
    user_list = user_list.drop_duplicates(subset=USER_LIST_COLUMNS)

    return user_list


def create_monthly_records(data: pd.DataFrame, target_month: str, user_list: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """利用者一覧から、その月のチェック行を作ります。"""
    new_records = []
    skipped_count = 0

    for _, row in user_list.iterrows():
        user_name = row["利用者名"]

        # 同じ月・同じ利用者がすでにあれば、二重登録しません。
        already_exists = (
            (data["対象月"] == target_month)
            & (data["利用者名"] == user_name)
        ).any()

        if already_exists:
            skipped_count += 1
            continue

        record = {"対象月": target_month, "利用者名": user_name}

        # 月初はすべて未確認から開始します。
        for item in CHECK_ITEMS:
            record[item] = False

        new_records.append(record)

    if new_records:
        data = pd.concat([data, pd.DataFrame(new_records)], ignore_index=True)

    return data, len(new_records), skipped_count


def create_single_record(target_month: str, user_name: str) -> dict:
    """1人分の新規登録データを作ります。"""
    record = {
        "対象月": target_month,
        "利用者名": user_name,
    }

    # 手入力で追加するときも、最初はすべて未確認にします。
    for item in CHECK_ITEMS:
        record[item] = False

    return record


# ==============================
# 画面表示
# ==============================

st.set_page_config(
    page_title="月次カルテ確認チェックリスト",
    layout="wide",
)

st.title("訪問看護ステーション 月次カルテ確認チェックリスト")
st.caption("ローカルではCSV、WebアプリではGoogleスプレッドシートに保存できます。")
st.caption(f"アプリ版: {APP_VERSION}")

# Googleスプレッドシート保存を使うWeb版では、利用者ごとのデータを分けるために
# Googleログインを必須にします。
require_login_for_google_sheets()

# 保存済みデータを読み込みます。
# Googleスプレッドシート設定がない場合は、自動的にローカルCSVを使います。
try:
    data = load_data()
except Exception as error:
    st.error("保存先の読み込みでエラーが発生しました。")
    st.exception(error)
    st.stop()

show_storage_status()

if has_google_sheets_settings() and is_user_logged_in():
    if st.button("ログアウト"):
        st.logout()

# 今日の日付から、対象月の初期値を作ります。
default_month = date.today().strftime("%Y-%m")


# ==============================
# 月次データの作成
# ==============================

st.header("月次データの作成")

with st.expander("利用者一覧CSVからまとめて作成", expanded=True):
    import_month = st.text_input(
        "作成する対象月",
        value=default_month,
        key="import_month",
        help="例: 2026-05",
    )

    uploaded_file = st.file_uploader(
        "利用者一覧CSVを選択",
        type=["csv"],
        help="CSVには「利用者名」の列を入れてください。",
    )

    if uploaded_file is not None:
        try:
            user_list = read_uploaded_user_list(uploaded_file)
            st.write("取り込み予定の利用者一覧")
            st.dataframe(user_list, use_container_width=True, hide_index=True)

            if st.button("この利用者一覧で月次データを作成"):
                if not import_month.strip():
                    st.error("対象月を入力してください。")
                else:
                    data, added_count, skipped_count = create_monthly_records(
                        data,
                        import_month.strip(),
                        user_list,
                    )
                    save_data(data)
                    st.success(
                        f"{added_count}件を追加しました。"
                        f"すでに登録済みのため {skipped_count}件はスキップしました。"
                    )
                    st.rerun()
        except ValueError as error:
            st.error(str(error))

with st.expander("1人ずつ手入力で追加"):
    with st.form("single_record_form", clear_on_submit=False):
        col1, col2 = st.columns(2)

        with col1:
            target_month = st.text_input("対象月", value=default_month, key="single_month")

        with col2:
            user_name = st.text_input("利用者名", placeholder="例: 佐藤 太郎")

        submitted = st.form_submit_button("未確認として追加")

        if submitted:
            if not target_month.strip() or not user_name.strip():
                st.error("対象月、利用者名を入力してください。")
            else:
                already_exists = (
                    (data["対象月"] == target_month.strip())
                    & (data["利用者名"] == user_name.strip())
                ).any()

                if already_exists:
                    st.warning("同じ対象月・利用者名の登録がすでにあります。")
                else:
                    new_record = create_single_record(
                        target_month.strip(),
                        user_name.strip(),
                    )
                    data = pd.concat([data, pd.DataFrame([new_record])], ignore_index=True)
                    save_data(data)
                    st.success("未確認データとして追加しました。")
                    st.rerun()


# ==============================
# 絞り込み
# ==============================

st.header("絞り込み")

if data.empty:
    st.info("まだデータがありません。月次データを作成してください。")
    filtered_data = data.copy()
else:
    month_options = ["すべて"] + sorted(data["対象月"].dropna().unique(), reverse=True)
    selected_month = st.selectbox("対象月で絞り込み", options=month_options)
    filtered_data = filter_data(data, selected_month)

    st.caption(f"表示中: {len(filtered_data)} 件 / 全体: {len(data)} 件")


# ==============================
# チェック結果の更新保存
# ==============================

st.header("チェック結果の更新保存")

if filtered_data.empty:
    st.info("更新できるデータがありません。")
else:
    # data_editorで編集したあと、元のCSVのどの行を更新するか分かるように、
    # 元の行番号を「保存用ID」として持たせます。
    editor_data = filtered_data.copy()
    editor_data.insert(0, "保存用ID", editor_data.index)
    editor_data = add_status_columns(editor_data)

    edited_data = st.data_editor(
        editor_data[["保存用ID", "対象月", "利用者名"] + CHECK_ITEMS + ["状況", "未確認項目"]],
        use_container_width=True,
        hide_index=True,
        disabled=["保存用ID", "対象月", "利用者名", "状況", "未確認項目"],
        column_config={
            "保存用ID": st.column_config.NumberColumn("ID", help="保存時に使う番号です。編集不要です。"),
        },
    )

    if st.button("編集したチェック結果を保存"):
        # チェック項目だけを元データへ戻します。
        for _, row in edited_data.iterrows():
            original_index = int(row["保存用ID"])
            for item in CHECK_ITEMS:
                data.at[original_index, item] = bool(row[item])

        save_data(data)
        st.success("チェック結果をCSVに保存しました。")
        st.rerun()


# ==============================
# 進捗確認
# ==============================

st.header("進捗確認")

if filtered_data.empty:
    st.info("進捗を表示できるデータがありません。")
else:
    total_users = len(filtered_data)
    display_data = add_status_columns(filtered_data)
    completed_users = int((display_data["状況"] == "完了").sum())
    incomplete_users = total_users - completed_users
    total_items = total_users * len(CHECK_ITEMS)
    checked_items = int(filtered_data[CHECK_ITEMS].sum().sum())
    progress_rate = checked_items / total_items if total_items else 0

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("利用者数", total_users)
    col2.metric("完了", completed_users)
    col3.metric("未完了", incomplete_users)
    col4.metric("進捗率", f"{progress_rate:.0%}")


# ==============================
# 月末の未完了一覧
# ==============================

st.header("月末の未完了一覧")

if data.empty:
    st.info("未完了一覧を表示できるデータがありません。")
else:
    report_month_options = sorted(data["対象月"].dropna().unique(), reverse=True)
    report_month = st.selectbox("月末確認する対象月", options=report_month_options)

    month_data = data[data["対象月"] == report_month].copy()
    incomplete_data = month_data[month_data[CHECK_ITEMS].eq(False).any(axis=1)].copy()

    if incomplete_data.empty:
        st.success(f"{report_month} はすべて確認完了です。")
    else:
        incomplete_display = add_status_columns(incomplete_data)
        st.warning(f"{report_month} の未完了は {len(incomplete_display)} 件です。")
        st.dataframe(
            incomplete_display[["対象月", "利用者名", "未確認項目", "状況"]],
            use_container_width=True,
            hide_index=True,
        )

        incomplete_csv = incomplete_display.to_csv(index=False, encoding="utf-8-sig")

        st.download_button(
            label="月末未完了一覧CSVをダウンロード",
            data=incomplete_csv,
            file_name=f"{report_month}_未完了一覧.csv",
            mime="text/csv",
        )


# ==============================
# 一覧表示とCSV出力
# ==============================

st.header("一覧表示とCSV出力")

if filtered_data.empty:
    st.info("表示できるデータがありません。")
else:
    display_data = add_status_columns(filtered_data)

    st.dataframe(
        display_data.style.apply(highlight_incomplete_rows, axis=1),
        use_container_width=True,
        hide_index=True,
    )

    filtered_csv = display_data.to_csv(index=False, encoding="utf-8-sig")

    st.download_button(
        label="表示中の一覧CSVをダウンロード",
        data=filtered_csv,
        file_name="karte_checklist_filtered.csv",
        mime="text/csv",
    )

    if has_google_sheets_settings():
        st.caption("保存先: Googleスプレッドシート")
    else:
        st.caption(f"保存先ファイル: {DATA_FILE}")


# ==============================
# 登録データの削除
# ==============================

st.header("登録データの削除")

if data.empty:
    st.info("削除できる登録データはありません。")
else:
    delete_options = {
        index: f'{row["対象月"]} / {row["利用者名"]}'
        for index, row in data.iterrows()
    }

    selected_index = st.selectbox(
        "削除する登録を選択",
        options=list(delete_options.keys()),
        format_func=lambda index: delete_options[index],
    )

    confirm_delete = st.checkbox("選択した登録データを削除することを確認しました")

    if st.button("選択した登録を削除", disabled=not confirm_delete):
        data = data.drop(index=selected_index).reset_index(drop=True)
        save_data(data)
        st.success("選択した登録データを削除しました。")
        st.rerun()
