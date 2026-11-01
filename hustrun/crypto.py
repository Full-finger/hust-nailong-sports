"""
加密模块 — 对应 BackAES / AESType / DeviceUtils2.client

实现：
- AES/ECB/PKCS5Padding 加密（mode 0）
- client 头加密（{root, t, heap} JSON → AES）
- WCDB 数据库解密辅助
"""

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


def aes_encrypt_ecb(plaintext: str, key: str) -> bytes:
    """
    AES/ECB/PKCS5Padding 加密。
    对应 BackAES.秪踖拽炅鮤幕諭紨伨(str, key, 0) — mode 0 = ECB。

    Args:
        plaintext: 明文字符串
        key: 密钥字符串，取 UTF-8 编码前 16 字节

    Returns:
        加密后的字节数组
    """
    key_bytes = key.encode("utf-8")[:16]
    cipher = AES.new(key_bytes, AES.MODE_ECB)
    plain_bytes = plaintext.encode("utf-8")
    return cipher.encrypt(pad(plain_bytes, AES.block_size))


def encrypt_for_upload(json_str: str, key: str, encoding: str = "hex") -> str:
    """
    加密 JSON 并编码为传输格式。

    对应: new String(BackAES.秪踖拽炅鮤幕諭紨伨(json, SECRET[:16], 0))

    注：原生层 (com.security.inner.apk0000.x) 实际实现未知，
    输出编码通过 encoding 参数可切换 hex / base64。

    Args:
        json_str: 要加密的 JSON 字符串
        key: AES 密钥（取前 16 字节）
        encoding: 输出编码 "hex" 或 "base64"

    Returns:
        编码后的密文字符串
    """
    encrypted = aes_encrypt_ecb(json_str, key)
    if encoding == "base64":
        import base64
        return base64.b64encode(encrypted).decode("ascii")
    else:
        return encrypted.hex()


def encrypt_client_header(
    key: str,
    is_root: bool = False,
    encoding: str = "hex",
) -> str:
    """
    生成 client 请求头（反作弊信标）。

    对应 DeviceUtils2.辎犾廖蝿早崢雑():
        AES( Gson({root, t, heap}), SECRET[:16], 0 )

    Args:
        key: AES 密钥
        is_root: 设备是否 root

    Returns:
        加密后的 client header 值
    """
    import time
    import json

    payload = json.dumps({
        # Gson 字段顺序由 DeviceBean 声明顺序决定。
        "heap": "0",
        "root": "1" if is_root else "0",
        "t": str(int(time.time() * 1000)),
    }, ensure_ascii=False, separators=(",", ":"))

    return encrypt_for_upload(payload, key, encoding)


def encrypt_sensor_data(sensor_text: str, key: str, encoding: str = "hex") -> str:
    """
    加密传感器数据用于 sensorFile 上传。

    对应 SportUploadModel.配鱯堑藖蠵璁慭灗():
        AES(sensorTxt, SECRET[:16], 0)

    Args:
        sensor_text: 传感器原始文本
        key: AES 密钥
        encoding: 输出编码

    Returns:
        加密后的传感器数据
    """
    return encrypt_for_upload(sensor_text, key, encoding)


def open_wcdb(db_path: str, wcdb_key: str = "d8c0521f8bd58cb5eae44f9c4bf647c5"):
    """
    打开 WCDB 加密数据库。

    使用 sqlcipher3 兼容模式：
        PRAGMA key = 'd8c0521f8bd58cb5eae44f9c4bf647c5';
        PRAGMA cipher_page_size = 1024;
        PRAGMA cipher_compatibility = 3;

    Args:
        db_path: 数据库文件路径
        wcdb_key: 32 字符十六进制密钥

    Returns:
        sqlite3.Connection 对象
    """
    try:
        from sqlcipher3 import dbapi2 as sqlcipher
    except ImportError:
        try:
            from pysqlcipher3 import dbapi2 as sqlcipher
        except ImportError as exc:
            raise RuntimeError(
                "读取 WCDB 需要 SQLCipher Python 驱动；标准库 sqlite3 不支持 PRAGMA key。"
                "请安装 sqlcipher3 或 pysqlcipher3。"
            ) from exc

    if not wcdb_key or "'" in wcdb_key:
        raise ValueError("WCDB 密钥为空或包含非法字符")

    conn = sqlcipher.connect(db_path)
    conn.execute(f"PRAGMA key = '{wcdb_key}';")
    conn.execute("PRAGMA cipher_page_size = 1024;")
    conn.execute("PRAGMA cipher_compatibility = 3;")

    # 验证：尝试列出表
    try:
        conn.execute("SELECT count(*) FROM sqlite_master;").fetchone()
    except Exception as e:
        conn.close()
        raise RuntimeError(
            f"无法解密数据库 {db_path}。"
            f"请确认密钥正确且已安装 sqlcipher3: {e}"
        ) from e

    return conn
