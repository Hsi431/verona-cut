"""Shared English / Traditional Chinese messages for the desktop application."""
import os

_language = os.environ.get("VERONA_LANGUAGE", "en")

CORE_ENGLISH = {
    "TCP 傳輸中斷：本機已接受 {sent}/{total} bytes；不會自動重送。原因：{reason}": "TCP transfer interrupted: the local socket accepted {sent}/{total} bytes; no automatic resend. Reason: {reason}",
    "對位微調必須是兩個介於 −5 至 5 mm 的有限數值": "Registration adjustment must contain two finite values from −5 to 5 mm",
    "對位微調只適用於 Print & Cut": "Registration adjustment only applies to Print & Cut",
    "微調後的路徑超出 Print & Cut 邊界": "The adjusted path extends outside the Print & Cut bounds",
    "無法讀取 LDS：{reason}": "Cannot read LDS: {reason}",
    "展開點數超過 {limit} 點，請簡化路徑或拆分文件後重試。": "Expanded point limit exceeded ({limit}); simplify paths or split the document and retry.",
    "資料紀錄超過 {limit} 筆，請拆分文件後重試。": "Record limit exceeded ({limit}); split the document and retry.",
    "物件巢狀超過 {limit} 層，請減少巢狀群組後重試。": "Object depth limit exceeded ({limit}); reduce nested groups and retry.",
    "結構物件超過 {limit} 個，請拆分文件後重試。": "Structural object limit exceeded ({limit}); split the document and retry.",
    "圖稿物件超過 {limit} 個，請拆分文件後重試。": "Artwork item limit exceeded ({limit}); split the document and retry.",
    "檔案超過 {limit} MiB，請拆分文件後重試。": "File exceeds {limit} MiB; split the document and retry.",
    "文件巢狀超過 {limit} 層，請減少巢狀後重試。": "Document depth exceeds {limit}; reduce nesting and retry.",
    "文件巢狀過深，無法讀取。": "Document nesting is too deep to read.",
    "JSON 結構過大，請拆分文件後重試。": "JSON structure is too large; split the document and retry.",
    "SVG 元素超過 {limit} 個，請拆分文件後重試。": "SVG element limit exceeded ({limit}); split the document and retry.",
    "SVG 路徑資料過大，請簡化或拆分文件後重試。": "SVG path data is too large; simplify or split the document and retry.",
    "SVG 包含無效座標或變形。": "SVG contains invalid coordinates or transforms.",
    "SVG 路徑資料無效，請檢查座標及曲線參數。": "Invalid SVG path data; check coordinates and curve parameters.",
    "LDS 包含尚未支援的物件類型：{code}": "The LDS contains an unsupported object type: {code}",
    "此 LDS 的多圖層或隱藏圖層尚未支援。": "Multiple or hidden LDS layers are not supported yet.",
    "此 LDS 的群組變形尚未支援。": "Transformed LDS groups are not supported yet.",
    "此 LDS 的圖片編碼尚未支援。": "This LDS image encoding is not supported yet.",
    "原生路徑 {number}": "Native path {number}",
    "原生圖片 {number}": "Native image {number}",
    "工具必須是刀片 (0) 或畫筆 (1)": "Tool must be blade (0) or pen (1)",
    "壓力": "Force",
    "速度": "Speed",
    "{label}必須介於 1–{upper}": "{label} must be between 1 and {upper}",
    "路徑包含無效座標": "The design contains invalid coordinates",
    "路徑超出 300 × 300 mm 工作區": "Paths extend outside the 300 × 300 mm workspace",
    "路徑長度超出機器座標範圍": "Path length exceeds the machine coordinate range",
    "設計中沒有可切割的線段": "The design has no cuttable line segments",
    "Print & Cut 邊界必須是四個有限數值": "Print & Cut bounds must contain four finite numbers",
    "Print & Cut 邊界必須為正數且尺寸不超過 300 mm": "Print & Cut bounds must be positive and no larger than 300 mm",
    "路徑超出 Print & Cut 邊界": "A path extends outside the Print & Cut bounds",
    "SVG 尺寸無法換算成毫米：{value}": "Cannot convert the SVG dimension to millimetres: {value}",
    "SVG 尺寸必須是正數": "SVG dimensions must be positive numbers",
    "請將 SVG 儲存為不含外部文件宣告的純 SVG": "Save as plain SVG without document type or entity declarations",
    "檔案不是 SVG": "The file is not an SVG document",
    "SVG 格式錯誤：{reason}": "The SVG is not well-formed XML: {reason}",
    "SVG viewBox 無效": "The SVG viewBox is invalid",
    "請使用 SVG 預設 preserveAspectRatio 或 none": "Use the default SVG preserveAspectRatio setting or none",
    "請先把 SVG 裁切遮罩、濾鏡轉成實際路徑": "Convert SVG clipping, masks and filters to actual paths first",
    "SVG 曲線過大；請先縮小設計": "The SVG curve is too large; reduce the design size first",
    "SVG 的 {tag} 尚未支援；請先在 Inkscape 轉成純向量路徑": "SVG {tag} is not supported; convert it to plain vector paths in Inkscape first",
    "曲線精度必須是正數": "Curve tolerance must be a positive number",
    "SVG 沒有可切割的向量路徑": "The SVG has no cuttable vector paths",
    "請輸入 Juliet 螢幕顯示的單一 IPv4 位址": "Enter the single IPv4 address shown on Juliet's screen",
    "連接埠必須介於 1–65535": "Port must be between 1 and 65535",
    "沒有可傳送的切割資料": "There is no cutting data to send",
    "所選 USB 裝置不符合支援的切割機識別碼": "The selected USB device does not match the supported cutter identifiers",
    "未找到唯一可用的 USB bulk 介面": "No unique supported USB bulk interface is available",
    "USB 傳輸未完成；不會自動重送已送出的資料": "USB transfer was incomplete; already sent data will not be retried automatically",
}


def set_language(language):
    global _language
    if language not in ("en", "zh_TW"):
        raise ValueError("Supported languages: en, zh_TW")
    _language = language
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is not None:
        from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
        previous = getattr(app, "_verona_translator", None)
        if previous is not None:
            app.removeTranslator(previous)
        app._verona_translator = None
        QLocale.setDefault(QLocale("en_US" if language == "en" else "zh_TW"))
        if language == "zh_TW":
            translator = QTranslator(app)
            directory = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
            if translator.load("qtbase_zh_TW", directory):
                app.installTranslator(translator)
                app._verona_translator = translator


def get_language():
    return _language if _language in ("en", "zh_TW") else "en"


def tr(message, **values):
    if get_language() == "en":
        from .ui_translations import ENGLISH
        message = ENGLISH.get(message, CORE_ENGLISH.get(message, message))
    return message.format(**values)
