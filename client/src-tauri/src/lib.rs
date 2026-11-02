use reqwest::{Client, Method, StatusCode};
use serde::{Deserialize, Serialize};
use std::{fs, path::PathBuf, time::Duration};
use tauri::{Manager, State};
use tokio::sync::Mutex;
use url::{Host, Url};

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "lowercase")]
enum Environment {
    Development,
    Production,
}

impl Environment {
    fn from_identifier(identifier: &str) -> Self {
        if identifier.ends_with(".dev") {
            Self::Development
        } else {
            Self::Production
        }
    }
    fn label(&self) -> &'static str {
        match self {
            Self::Development => "开发环境",
            Self::Production => "正式环境",
        }
    }
}

#[derive(Clone, Serialize)]
struct AppError {
    code: String,
    message: String,
    #[serde(skip)]
    status: Option<u16>,
}
type AppResult<T> = Result<T, AppError>;

fn error(code: &str, message: impl Into<String>) -> AppError {
    AppError {
        code: code.into(),
        message: message.into(),
        status: None,
    }
}

#[derive(Clone, Deserialize, Serialize)]
struct ConnectionConfig {
    schema_version: u8,
    environment: Environment,
    base_url: String,
    #[serde(default)]
    restore_session: bool,
}

#[derive(Clone, Deserialize, Serialize)]
struct SystemInfo {
    environment: Environment,
    instance_id: String,
    app_version: String,
    api_version: String,
    ready: bool,
    reason: Option<String>,
}

#[derive(Clone, Deserialize, Serialize)]
struct SessionInfo {
    username: String,
    expires_at: String,
}

#[derive(Clone, Deserialize, Serialize)]
struct WorkspaceInfo {
    message: String,
    environment: Environment,
    instance_id: String,
    username: String,
    database: String,
}

// This type never leaves Rust. It is persisted only in the platform credential store.
#[derive(Clone, Deserialize, Serialize)]
struct Credential {
    token: String,
    base_url: String,
    environment: Environment,
    instance_id: String,
}

#[derive(Default)]
struct Runtime {
    config: Option<ConnectionConfig>,
    system: Option<SystemInfo>,
    session: Option<SessionInfo>,
    workspace: Option<WorkspaceInfo>,
    credential: Option<Credential>,
    notice: Option<String>,
}

struct AppState {
    environment: Environment,
    config_file: PathBuf,
    keyring_service: String,
    client: Client,
    runtime: Mutex<Runtime>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ClientSnapshot {
    environment: Environment,
    base_url: Option<String>,
    system: Option<SystemInfo>,
    session: Option<SessionInfo>,
    workspace: Option<WorkspaceInfo>,
    notice: Option<String>,
}

impl AppState {
    fn snapshot(&self, runtime: &Runtime) -> ClientSnapshot {
        ClientSnapshot {
            environment: self.environment.clone(),
            base_url: runtime.config.as_ref().map(|v| v.base_url.clone()),
            system: runtime.system.clone(),
            session: runtime.session.clone(),
            workspace: runtime.workspace.clone(),
            notice: runtime.notice.clone(),
        }
    }
    fn write_config(&self, config: &ConnectionConfig) -> AppResult<()> {
        let parent = self
            .config_file
            .parent()
            .ok_or_else(|| error("CONFIG_WRITE_FAILED", "无法确定连接配置目录。"))?;
        fs::create_dir_all(parent).map_err(|_| {
            error(
                "CONFIG_WRITE_FAILED",
                "无法创建连接配置目录，请检查本机目录权限。",
            )
        })?;
        let bytes = serde_json::to_vec_pretty(config)
            .map_err(|_| error("CONFIG_WRITE_FAILED", "无法保存连接配置。"))?;
        let temporary = self.config_file.with_extension("json.tmp");
        fs::write(&temporary, bytes)
            .and_then(|_| fs::rename(&temporary, &self.config_file))
            .map_err(|_| {
                error(
                    "CONFIG_WRITE_FAILED",
                    "连接配置保存失败，请检查目录权限和磁盘空间。",
                )
            })
    }
}

fn normalize_base_url(input: &str) -> AppResult<String> {
    let url = Url::parse(input.trim()).map_err(|_| {
        error(
            "INVALID_URL",
            "请输入完整服务地址，例如 https://192.168.1.20:8443。",
        )
    })?;
    if url.host().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/"
    {
        return Err(error(
            "INVALID_URL",
            "服务地址只填写协议、主机和端口，不包含账号、路径、查询参数或片段。",
        ));
    }
    let loopback = match url.host() {
        Some(Host::Ipv4(ip)) => ip.is_loopback(),
        Some(Host::Ipv6(ip)) => ip.is_loopback(),
        Some(Host::Domain(name)) => name == "localhost",
        None => false,
    };
    match url.scheme() {
        "https" => Ok(url.as_str().trim_end_matches('/').to_string()),
        "http" if loopback => Ok(url.as_str().trim_end_matches('/').to_string()),
        "http" => Err(error(
            "HTTPS_REQUIRED",
            "局域网服务必须使用 HTTPS。仅本机回环地址允许 HTTP，请配置可信证书。",
        )),
        _ => Err(error(
            "INVALID_URL",
            "服务地址必须使用 HTTPS；本机开发可使用 HTTP 回环地址。",
        )),
    }
}

#[derive(Serialize)]
struct LoginPayload<'a> {
    username: &'a str,
    password: &'a str,
    device_name: &'a str,
}

enum Request<'a> {
    Health,
    System,
    Login(LoginPayload<'a>),
    Session,
    Logout,
    Workspace,
}

async fn request(
    app: &AppState,
    base_url: &str,
    route: Request<'_>,
    token: Option<&str>,
) -> AppResult<serde_json::Value> {
    let (method, path) = match &route {
        Request::Health => (Method::GET, "/health"),
        Request::System => (Method::GET, "/api/v1/system"),
        Request::Login(_) => (Method::POST, "/api/v1/auth/login"),
        Request::Session => (Method::GET, "/api/v1/auth/session"),
        Request::Logout => (Method::POST, "/api/v1/auth/logout"),
        Request::Workspace => (Method::GET, "/api/v1/workspace"),
    };
    let mut builder = app.client.request(method, format!("{base_url}{path}"));
    if let Some(token) = token {
        builder = builder.bearer_auth(token);
    }
    if let Request::Login(payload) = route {
        builder = builder.json(&payload);
    }
    let response = builder.send().await.map_err(|failure| {
        if failure.is_timeout() {
            error(
                "CONNECTION_TIMEOUT",
                "服务请求超时，请检查网络和服务状态后重试。",
            )
        } else {
            error(
                "CONNECTION_FAILED",
                "无法建立可信连接，请检查服务地址、网络及系统信任的 HTTPS 证书。",
            )
        }
    })?;
    let status = response.status();
    if status.is_redirection() {
        return Err(error(
            "REDIRECT_NOT_ALLOWED",
            "服务返回了重定向。请填写服务最终地址，客户端不会跟随跳转。",
        ));
    }
    if status == StatusCode::NO_CONTENT {
        return Ok(serde_json::Value::Null);
    }
    let body = response.json::<serde_json::Value>().await.map_err(|_| {
        error(
            "INVALID_RESPONSE",
            "服务返回内容不是兼容的 JSON，请确认地址指向采购对账服务。",
        )
    })?;
    if !status.is_success() {
        let code = body
            .pointer("/error/code")
            .and_then(|v| v.as_str())
            .unwrap_or("SERVICE_ERROR");
        let message = body
            .pointer("/error/message")
            .and_then(|v| v.as_str())
            .unwrap_or("服务未能完成请求，请检查输入后重试。");
        let mut failure = error(code, message);
        failure.status = Some(status.as_u16());
        return Err(failure);
    }
    Ok(body)
}

fn decode<T: serde::de::DeserializeOwned>(value: serde_json::Value) -> AppResult<T> {
    serde_json::from_value(value).map_err(|_| {
        error(
            "INCOMPATIBLE_RESPONSE",
            "服务响应字段与客户端不兼容，请检查服务和客户端版本。",
        )
    })
}

fn validate_system(system: &SystemInfo, environment: &Environment) -> AppResult<()> {
    if &system.environment != environment {
        return Err(error(
            "ENVIRONMENT_MISMATCH",
            format!(
                "此客户端属于{}，服务返回了{}。请切换为匹配的服务地址。",
                environment.label(),
                system.environment.label()
            ),
        ));
    }
    if system.api_version != "1" {
        return Err(error(
            "API_VERSION_MISMATCH",
            "服务 API 版本不兼容；此客户端需要 API v1。",
        ));
    }
    if !system.ready {
        return Err(error(
            "SERVICE_NOT_READY",
            format!(
                "服务尚未就绪：{}",
                system
                    .reason
                    .as_deref()
                    .unwrap_or("请管理员检查数据库和服务初始化状态。")
            ),
        ));
    }
    if system.instance_id.trim().is_empty() {
        return Err(error(
            "INVALID_INSTANCE",
            "服务未返回有效实例标识，无法安全建立会话。",
        ));
    }
    Ok(())
}

fn base_url(runtime: &Runtime) -> AppResult<String> {
    runtime
        .config
        .as_ref()
        .map(|v| v.base_url.clone())
        .ok_or_else(|| error("CONNECTION_REQUIRED", "请先在连接设置中保存服务地址。"))
}

fn clear_session(runtime: &mut Runtime) {
    runtime.credential = None;
    runtime.session = None;
    runtime.workspace = None;
}

#[cfg(any(target_os = "macos", target_os = "windows"))]
fn store_credential(service: &str, credential: &Credential) -> Result<(), ()> {
    let value = serde_json::to_string(credential).map_err(|_| ())?;
    keyring::Entry::new(service, "session")
        .map_err(|_| ())?
        .set_password(&value)
        .map_err(|_| ())
}
#[cfg(any(target_os = "macos", target_os = "windows"))]
fn load_credential(service: &str) -> Option<Credential> {
    let value = keyring::Entry::new(service, "session")
        .ok()?
        .get_password()
        .ok()?;
    serde_json::from_str(&value).ok()
}
#[cfg(any(target_os = "macos", target_os = "windows"))]
fn delete_credential(service: &str) -> bool {
    match keyring::Entry::new(service, "session").and_then(|v| v.delete_credential()) {
        Ok(()) | Err(keyring::Error::NoEntry) => true,
        Err(_) => false,
    }
}
#[cfg(not(any(target_os = "macos", target_os = "windows")))]
fn store_credential(_: &str, _: &Credential) -> Result<(), ()> {
    Err(())
}
#[cfg(not(any(target_os = "macos", target_os = "windows")))]
fn load_credential(_: &str) -> Option<Credential> {
    None
}
#[cfg(not(any(target_os = "macos", target_os = "windows")))]
fn delete_credential(_: &str) -> bool {
    true
}

async fn checked_system(app: &AppState, runtime: &mut Runtime) -> AppResult<SystemInfo> {
    let base = base_url(runtime)?;
    let result = async {
        let system: SystemInfo = decode(request(app, &base, Request::System, None).await?)?;
        validate_system(&system, &app.environment)?;
        Ok(system)
    }
    .await;
    match result {
        Ok(system) => {
            if runtime
                .credential
                .as_ref()
                .is_some_and(|c| c.instance_id != system.instance_id)
            {
                clear_session(runtime);
                runtime.system = Some(system);
                return Err(error(
                    "INSTANCE_CHANGED",
                    "服务实例已更换，旧会话已在客户端清除。请重新登录。",
                ));
            }
            runtime.system = Some(system.clone());
            Ok(system)
        }
        Err(failure) => {
            clear_session(runtime);
            runtime.system = None;
            Err(failure)
        }
    }
}

async fn verify_instance_after_response(
    app: &AppState,
    runtime: &mut Runtime,
    expected_instance: &str,
) -> AppResult<()> {
    let base = base_url(runtime)?;
    let verification: AppResult<()> = async {
        let current: SystemInfo = decode(request(app, &base, Request::System, None).await?)?;
        validate_system(&current, &app.environment)?;
        if current.instance_id != expected_instance {
            return Err(error(
                "INSTANCE_CHANGED",
                "请求期间服务实例发生变化，未保存会话。请重新检查连接。",
            ));
        }
        Ok(())
    }
    .await;
    if verification.is_err() {
        clear_session(runtime);
        runtime.system = None;
    }
    verification
}

async fn workspace(app: &AppState, runtime: &mut Runtime) -> AppResult<()> {
    let system = checked_system(app, runtime).await?;
    let base = base_url(runtime)?;
    let token = runtime
        .credential
        .as_ref()
        .map(|c| c.token.clone())
        .ok_or_else(|| error("LOGIN_REQUIRED", "会话不可用，请重新登录。"))?;
    let result = request(app, &base, Request::Workspace, Some(&token)).await;
    let value: WorkspaceInfo = match result {
        Ok(value) => decode(value)?,
        Err(failure) => {
            // Authentication errors invalidate local context; transient workspace errors remain retryable.
            if failure.status == Some(401) {
                clear_session(runtime);
                delete_credential(&app.keyring_service);
            }
            return Err(failure);
        }
    };
    if value.environment != app.environment
        || value.instance_id != system.instance_id
        || runtime
            .session
            .as_ref()
            .is_none_or(|session| session.username != value.username)
    {
        clear_session(runtime);
        runtime.system = None;
        return Err(error(
            "INSTANCE_CHANGED",
            "服务概况与已验证实例不一致，已清除客户端会话，请重新检查连接。",
        ));
    }
    runtime.workspace = Some(value);
    Ok(())
}

#[tauri::command]
async fn bootstrap(app: State<'_, AppState>) -> AppResult<ClientSnapshot> {
    let runtime = app.runtime.lock().await;
    Ok(app.snapshot(&runtime))
}

#[tauri::command(rename_all = "camelCase")]
async fn save_connection(app: State<'_, AppState>, base_url: String) -> AppResult<ClientSnapshot> {
    let base = normalize_base_url(&base_url)?;
    let mut runtime = app.runtime.lock().await;
    let config = ConnectionConfig {
        schema_version: 1,
        environment: app.environment.clone(),
        base_url: base,
        restore_session: false,
    };
    app.write_config(&config)?;
    clear_session(&mut runtime);
    runtime.system = None;
    runtime.config = Some(config);
    runtime.notice = Some(
        if delete_credential(&app.keyring_service) {
            "连接地址已保存，请检查服务连接。"
        } else {
            "连接已切换；旧凭据未能从系统钥匙串删除，自动恢复已禁用。"
        }
        .into(),
    );
    Ok(app.snapshot(&runtime))
}

#[tauri::command]
async fn connect_service(app: State<'_, AppState>) -> AppResult<ClientSnapshot> {
    let mut runtime = app.runtime.lock().await;
    let memory_credential = runtime.credential.clone();
    clear_session(&mut runtime);
    runtime.system = None;
    runtime.notice = None;
    let base = base_url(&runtime)?;
    request(&app, &base, Request::Health, None).await?;
    let system = checked_system(&app, &mut runtime).await?;
    let saved_credential = memory_credential.or_else(|| {
        if runtime.config.as_ref().is_some_and(|v| v.restore_session) {
            load_credential(&app.keyring_service)
        } else {
            None
        }
    });
    if let Some(credential) = saved_credential {
        if credential.base_url == base
            && credential.environment == app.environment
            && credential.instance_id == system.instance_id
        {
            match request(&app, &base, Request::Session, Some(&credential.token))
                .await
                .and_then(decode::<SessionInfo>)
            {
                Ok(session) => {
                    verify_instance_after_response(&app, &mut runtime, &system.instance_id).await?;
                    runtime.credential = Some(credential);
                    runtime.session = Some(session);
                    if let Err(failure) = workspace(&app, &mut runtime).await {
                        runtime.notice = Some(failure.message);
                    }
                }
                Err(_) => {
                    delete_credential(&app.keyring_service);
                    runtime.notice = Some("已保存的会话不可用，请重新登录。".into());
                }
            }
        } else {
            delete_credential(&app.keyring_service);
        }
    }
    Ok(app.snapshot(&runtime))
}

#[derive(Deserialize)]
struct LoginUser {
    username: String,
}
#[derive(Deserialize)]
struct LoginResponse {
    token: String,
    expires_at: String,
    user: LoginUser,
}

#[tauri::command]
async fn login(
    app: State<'_, AppState>,
    username: String,
    password: String,
) -> AppResult<ClientSnapshot> {
    if username.trim().is_empty() || password.is_empty() {
        return Err(error("LOGIN_FIELDS_REQUIRED", "请填写管理员账号和密码。"));
    }
    let mut runtime = app.runtime.lock().await;
    if runtime.system.is_none() {
        return Err(error(
            "CONNECTION_NOT_CHECKED",
            "请先检查服务连接，再登录。",
        ));
    }
    let system = checked_system(&app, &mut runtime).await?;
    let base = base_url(&runtime)?;
    let device_name = format!("采购对账 · {} 桌面端", std::env::consts::OS);
    let response: LoginResponse = decode(
        request(
            &app,
            &base,
            Request::Login(LoginPayload {
                username: username.trim(),
                password: &password,
                device_name: &device_name,
            }),
            None,
        )
        .await?,
    )?;
    if response.token.is_empty() {
        return Err(error(
            "INVALID_RESPONSE",
            "服务未返回有效会话，请联系管理员检查登录接口。",
        ));
    }
    // Verify again before retaining a newly returned token: the server may have
    // restarted or the address may have been repointed while login was in flight.
    verify_instance_after_response(&app, &mut runtime, &system.instance_id).await?;
    let credential = Credential {
        token: response.token,
        base_url: base,
        environment: app.environment.clone(),
        instance_id: system.instance_id,
    };
    runtime.session = Some(SessionInfo {
        username: response.user.username,
        expires_at: response.expires_at,
    });
    runtime.workspace = None;
    runtime.notice = None;
    let stored = store_credential(&app.keyring_service, &credential).is_ok();
    runtime.credential = Some(credential);
    if let Some(config) = runtime.config.as_mut() {
        config.restore_session = stored;
        if app.write_config(config).is_err() {
            runtime.notice =
                Some("已登录，但会话恢复设置无法保存；退出应用后可能需要重新登录。".into());
        }
    }
    if !stored {
        runtime.notice =
            Some("已登录。系统凭据存储暂不可用，本次会话仅保留在原生进程内存中。".into());
    }
    if let Err(failure) = workspace(&app, &mut runtime).await {
        runtime.notice = Some(failure.message);
    }
    Ok(app.snapshot(&runtime))
}

#[tauri::command]
async fn refresh_workspace(app: State<'_, AppState>) -> AppResult<ClientSnapshot> {
    let mut runtime = app.runtime.lock().await;
    runtime.workspace = None;
    runtime.notice = None;
    workspace(&app, &mut runtime).await?;
    Ok(app.snapshot(&runtime))
}

#[tauri::command]
async fn logout(app: State<'_, AppState>) -> AppResult<ClientSnapshot> {
    let mut runtime = app.runtime.lock().await;
    let mut remote_done = false;
    if runtime.credential.is_some() && checked_system(&app, &mut runtime).await.is_ok() {
        if let (Ok(base), Some(credential)) = (base_url(&runtime), runtime.credential.as_ref()) {
            remote_done = request(&app, &base, Request::Logout, Some(&credential.token))
                .await
                .is_ok();
        }
    }
    clear_session(&mut runtime);
    let removed = delete_credential(&app.keyring_service);
    let mut config_saved = true;
    if let Some(config) = runtime.config.as_mut() {
        config.restore_session = false;
        config_saved = app.write_config(config).is_ok();
    }
    runtime.notice = Some(
        if !removed || !config_saved {
            "当前客户端已退出，但本机凭据清理未全部成功。请检查系统钥匙串与配置目录权限。"
        } else if !remote_done {
            "本机已退出并禁用会话恢复；服务暂不可达，原服务会话将按有效期失效。"
        } else {
            "已退出登录。后台服务继续运行。"
        }
        .into(),
    );
    Ok(app.snapshot(&runtime))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let environment = Environment::from_identifier(&app.config().identifier);
            let config_file = app.path().app_config_dir()?.join("connection.json");
            let mut runtime = Runtime::default();
            match fs::read(&config_file) {
                Ok(bytes) => match serde_json::from_slice::<ConnectionConfig>(&bytes) {
                    Ok(mut config)
                        if config.schema_version == 1 && config.environment == environment =>
                    {
                        match normalize_base_url(&config.base_url) {
                            Ok(base) => {
                                config.base_url = base;
                                runtime.config = Some(config);
                            }
                            Err(_) => {
                                runtime.notice =
                                    Some("已保存的地址不符合连接规则，请重新设置。".into())
                            }
                        }
                    }
                    _ => {
                        runtime.notice =
                            Some("连接配置无法读取或环境不匹配，请重新设置服务地址。".into())
                    }
                },
                Err(failure) if failure.kind() == std::io::ErrorKind::NotFound => {
                    if environment == Environment::Development {
                        runtime.config = Some(ConnectionConfig {
                            schema_version: 1,
                            environment: environment.clone(),
                            base_url: "http://127.0.0.1:18081".into(),
                            restore_session: false,
                        });
                    }
                }
                Err(_) => {
                    runtime.notice = Some("无法读取连接配置，请检查本机配置目录权限。".into())
                }
            }
            let client = Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .connect_timeout(Duration::from_secs(5))
                .timeout(Duration::from_secs(12))
                .build()?;
            app.manage(AppState {
                environment,
                config_file,
                keyring_service: format!("{}.session", app.config().identifier),
                client,
                runtime: Mutex::new(runtime),
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            bootstrap,
            save_connection,
            connect_service,
            login,
            refresh_workspace,
            logout
        ])
        .run(tauri::generate_context!())
        .expect("桌面应用无法启动");
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validates_tls_and_loopback_boundaries() {
        assert_eq!(
            normalize_base_url(" http://127.0.0.1:18081/ ").unwrap_or_default(),
            "http://127.0.0.1:18081"
        );
        assert!(normalize_base_url("http://localhost:18081").is_ok());
        assert!(normalize_base_url("http://[::1]:18081").is_ok());
        assert!(normalize_base_url("https://192.168.1.20:8443").is_ok());
        for unsafe_url in [
            "http://192.168.1.20:8080",
            "https://user:pass@example.test",
            "https://example.test/api",
            "https://example.test/?token=secret",
            "https://example.test/#fragment",
            "file:///tmp/service",
        ] {
            assert!(
                normalize_base_url(unsafe_url).is_err(),
                "should reject {unsafe_url}"
            );
        }
    }
    #[test]
    fn rejects_wrong_environment_and_api_even_when_ready() {
        let mut system = SystemInfo {
            environment: Environment::Production,
            instance_id: "instance-1".into(),
            app_version: "0.1.0".into(),
            api_version: "1".into(),
            ready: true,
            reason: None,
        };
        assert!(validate_system(&system, &Environment::Development).is_err());
        system.environment = Environment::Development;
        system.api_version = "2".into();
        assert!(validate_system(&system, &Environment::Development).is_err());
        system.api_version = "1".into();
        system.ready = false;
        assert!(validate_system(&system, &Environment::Development).is_err());
        assert_eq!(
            Environment::from_identifier("com.procurereconcile.desktop.dev"),
            Environment::Development
        );
    }
}
