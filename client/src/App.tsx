import { useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import {
  ArrowLeft, ArrowRight, ArrowUpRight, Blocks, Check, ChevronRight, CircleHelp,
  ClipboardList, Database, FileCheck2, Files, KeyRound, LayoutDashboard, Link2,
  LoaderCircle, LockKeyhole, LogOut, Monitor, PackageCheck, RefreshCw, Server,
  Settings2, ShieldCheck, TriangleAlert, Unplug, UserRound, X,
} from 'lucide-react';
import { describeError, formatEnvironment, formatExpiry, hasNativeBridge, nativeCommand } from './native';
import type { ClientError, ClientSnapshot } from './native';
import appIcon from '../src-tauri/icons/icon.svg';

const navigation = [
  { id: 'overview', label: '工作台', icon: LayoutDashboard, section: '工作空间' },
  { id: 'orders', label: '采购订单', icon: ClipboardList, description: '集中记录采购约定，查看订单行与交付进度。' },
  { id: 'receipts', label: '收货管理', icon: PackageCheck, description: '按订单登记实际到货、接受数量与问题数量。' },
  { id: 'returns', label: '退货管理', icon: ArrowUpRight, description: '关联来源收货，保留退回、换货和后续处置记录。' },
  { id: 'reconciliation', label: '供应商对账', icon: FileCheck2, section: '核对与单据', description: '关联业务记录，逐项核对供应商账单与差异。' },
  { id: 'invoices', label: '发票管理', icon: Files, description: '独立记录发票与核对状态，追溯相关业务和确认依据。' },
  { id: 'documents', label: '单据中心', icon: Blocks, description: '导入 Excel、PDF 和图片，复核字段与来源证据。' },
  { id: 'settings', label: '连接设置', icon: Settings2, section: '设置' },
];

function Status({ children, tone = 'neutral', dot = false }: {
  children: ReactNode; tone?: 'neutral' | 'success' | 'warning'; dot?: boolean;
}) {
  return <span className={`status status-${tone}`}>{dot && <span className="status-dot" />}{children}</span>;
}

function App() {
  const native = hasNativeBridge();
  const [page, setPage] = useState('overview');
  const [snapshot, setSnapshot] = useState<ClientSnapshot | null>(null);
  const [initializing, setInitializing] = useState(native);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<ClientError | null>(null);
  const [address, setAddress] = useState('');
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [checkedAt, setCheckedAt] = useState<Date | null>(null);
  const initialized = useRef(false);
  const actionLock = useRef(false);

  useEffect(() => {
    if (!native || initialized.current) return;
    initialized.current = true;
    nativeCommand('bootstrap').then((result) => {
      setSnapshot(result);
      setAddress(result.baseUrl ?? '');
    }).catch((failure: unknown) => setError(describeError(failure)))
      .finally(() => setInitializing(false));
  }, [native]);

  async function perform(label: string, action: () => Promise<ClientSnapshot>, onSuccess?: () => void) {
    if (actionLock.current) return;
    actionLock.current = true;
    setBusy(label);
    setError(null);
    try {
      const result = await action();
      setSnapshot(result);
      onSuccess?.();
    } catch (failure) {
      setError(describeError(failure));
      try { setSnapshot(await nativeCommand('bootstrap')); } catch { /* Keep the actionable initial failure. */ }
    } finally {
      actionLock.current = false;
      setBusy(null);
    }
  }

  function submitConnection(event: FormEvent) {
    event.preventDefault();
    void perform('connect', async () => {
      const normalizedInput = address.trim().replace(/\/$/, '');
      if (normalizedInput !== snapshot?.baseUrl) await nativeCommand('save_connection', { baseUrl: address });
      const result = await nativeCommand('connect_service');
      setAddress(result.baseUrl ?? address);
      setCheckedAt(new Date());
      return result;
    }, () => setPassword(''));
  }

  function submitLogin(event: FormEvent) {
    event.preventDefault();
    const submittedPassword = password;
    setPassword('');
    void perform('login', () => nativeCommand('login', { username, password: submittedPassword }));
  }

  const connected = Boolean(snapshot?.system?.ready);
  const session = snapshot?.session;
  const workspace = snapshot?.workspace;
  const selected = navigation.find((item) => item.id === page)!;
  const disabled = !native || Boolean(busy) || initializing;
  const connectionChanged = address.trim().replace(/\/$/, '') !== (snapshot?.baseUrl ?? '');
  const verified = connected && !connectionChanged;
  const notImplemented = page !== 'overview' && page !== 'settings';
  const checkedTime = checkedAt ? new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(checkedAt) : null;

  function connectionCard() {
    return <section className="panel connection-panel" aria-labelledby="connection-title">
      <div className="panel-heading">
        <span className={`step-mark ${verified ? 'step-done' : ''}`}>{verified ? <Check size={16} /> : '01'}</span>
        <div><h2 id="connection-title">连接服务</h2><p>选择当前工作台使用的业务服务</p></div>
        <Status tone={verified ? 'success' : 'neutral'} dot>{connectionChanged ? '待验证' : verified ? '已连接' : '未连接'}</Status>
      </div>
      <form onSubmit={submitConnection} className="connection-form">
        <label htmlFor="service-address">服务地址</label>
        <div className="input-with-icon"><Link2 size={17} />
          <input id="service-address" type="url" autoComplete="url" placeholder="https://192.168.1.20:8443"
            value={address} onChange={(event) => setAddress(event.target.value)} disabled={disabled}
            spellCheck={false} aria-describedby="address-hint" required />
        </div>
        <p className="field-hint" id="address-hint">本机开发使用回环地址；其他设备使用部署 Mac 的局域网 HTTPS 地址。</p>
        <div className="connection-check"><ShieldCheck size={18} />
          <div><strong>{verified ? '服务验证通过' : '连接前检查'}</strong>
            <span>{connectionChanged ? '地址已修改，请重新验证后登录。' : verified ? `环境与接口版本匹配${checkedTime ? ` · ${checkedTime} 检查` : ''}` : '验证服务可用性、运行环境与接口版本。'}</span>
          </div>
        </div>
        <button className="button button-primary" disabled={disabled || !address.trim()} type="submit">
          {busy === 'connect' ? <LoaderCircle size={16} className="spin" /> : <Link2 size={16} />}
          {busy === 'connect' ? '正在验证…' : connectionChanged ? '保存并验证连接' : '验证连接'}
          <ArrowRight size={16} className="button-trailing" />
        </button>
      </form>
      <div className="panel-footnote"><LockKeyhole size={14} /><span>更换服务地址后，需要重新登录</span></div>
    </section>;
  }

  function loginCard() {
    return <section className="panel login-panel" aria-labelledby="login-title">
      <div className="panel-heading">
        <span className={`step-mark ${session ? 'step-done' : ''}`}>{session ? <Check size={16} /> : '02'}</span>
        <div><h2 id="login-title">{session ? '当前会话' : '管理员登录'}</h2><p>{session ? '管理当前账号的登录状态' : '连接验证通过后，登录你的账号'}</p></div>
        {session ? <Status tone="success" dot>已登录</Status> : <LockKeyhole size={17} className="heading-icon" />}
      </div>
      {session ? <div className="session-body">
        <div className="session-identity"><span className="session-avatar"><UserRound size={24} /></span>
          <div><h3>{session.username}</h3><p>管理员</p></div><ShieldCheck size={19} className="session-check" />
        </div>
        <dl className="session-details"><div><dt>会话有效至</dt><dd>{formatExpiry(session.expires_at)}</dd></div>
          <div><dt>当前环境</dt><dd>{snapshot ? formatEnvironment(snapshot.environment) : '等待识别'}</dd></div>
        </dl>
        <button className="button button-secondary" disabled={disabled}
          onClick={() => { setPassword(''); void perform('logout', () => nativeCommand('logout')); }}>
          {busy === 'logout' ? <LoaderCircle size={16} className="spin" /> : <LogOut size={16} />}退出登录
        </button>
      </div> : <form className="login-form" onSubmit={submitLogin}>
        <div className="field"><label htmlFor="username">管理员账号</label>
          <div className="input-with-icon"><UserRound size={17} /><input id="username" autoComplete="username"
            value={username} onChange={(event) => setUsername(event.target.value)} disabled={disabled || !verified} required /></div>
        </div>
        <div className="field"><label htmlFor="password">密码</label>
          <div className="input-with-icon"><KeyRound size={17} /><input id="password" type="password" autoComplete="current-password"
            placeholder="输入管理员密码" value={password} onChange={(event) => setPassword(event.target.value)} disabled={disabled || !verified} required /></div>
        </div>
        <button className="button button-primary" type="submit" disabled={disabled || !verified || !username.trim() || !password}>
          {busy === 'login' ? <LoaderCircle size={16} className="spin" /> : <UserRound size={16} />}
          {busy === 'login' ? '正在登录…' : '登录工作台'}<ArrowRight size={16} className="button-trailing" />
        </button>
      </form>}
      <div className="panel-footnote"><ShieldCheck size={14} />
        <span>{session ? '会话由系统凭据存储保护' : verified ? '连接已就绪，可以登录' : '请先完成服务连接验证'}</span>
      </div>
    </section>;
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <a href="#" className="brand" onClick={(event) => { event.preventDefault(); setPage('overview'); }} aria-label="采购对账工作台首页">
        <span className="brand-symbol"><img src={appIcon} alt="" /></span>
        <span><strong>采购对账</strong><small>采购与供应商对账</small></span>
      </a>
      <nav aria-label="主导航">{navigation.map((item) => <div key={item.id}>
        {item.section && <div className="nav-section">{item.section}</div>}
        <button onClick={() => { setPage(item.id); setError(null); }} className={`nav-item ${page === item.id ? 'active' : ''}`}
          aria-current={page === item.id ? 'page' : undefined}>
          <item.icon size={18} strokeWidth={1.7} /><span>{item.label}</span>
        </button>
      </div>)}</nav>
      <div className="sidebar-bottom">
        <div className="desktop-label"><Monitor size={16} /><span>桌面客户端</span><span className="version">v0.1.0</span></div>
        <div className="sidebar-service"><span className={`connection-dot ${connected ? 'is-connected' : ''}`} />{connected ? '服务已连接' : '服务未连接'}</div>
      </div>
    </aside>

    <div className="main-shell">
      <header className="topbar">
        <div className="breadcrumb"><span>工作空间</span><ChevronRight size={14} /><strong>{selected.label}</strong></div>
        <div className="topbar-right">
          <Status tone={snapshot?.environment === 'development' ? 'warning' : 'neutral'} dot>
            {snapshot ? formatEnvironment(snapshot.environment) : native ? '环境待识别' : '界面预览'}
          </Status>
          {session && <><span className="topbar-divider" /><span className="user-chip"><span className="user-avatar"><UserRound size={14} /></span>{session.username}</span></>}
        </div>
      </header>

      <main><div className="page-content">
        <div className="page-heading">
          <div><h1>{selected.label}</h1><p>{notImplemented ? selected.description : page === 'settings'
            ? '管理服务地址，查看当前客户端的连接环境。' : session
              ? '查看服务与登录状态，准备开始今天的工作。' : '连接业务服务，登录后开始工作。'}</p></div>
          {page === 'overview' && <button className="button button-secondary button-small" onClick={() => setPage('settings')}><Settings2 size={16} />连接设置</button>}
        </div>

        {!native && <div className="message message-preview" role="status"><Monitor size={17} />
          <p><strong>界面预览</strong><span>连接服务与登录，请使用桌面客户端。</span></p></div>}
        {error && <div className="message message-error" role="alert"><TriangleAlert size={18} />
          <div><strong>操作未完成</strong><p>{error.message}</p><small>错误代码：{error.code}</small></div>
          <button className="icon-button message-dismiss" onClick={() => setError(null)} aria-label="关闭错误提示"><X size={16} /></button></div>}
        {snapshot?.notice && <div className="message message-info" role="status"><CircleHelp size={18} /><p>{snapshot.notice}</p></div>}
        {initializing && <div className="initializing" role="status"><LoaderCircle size={17} className="spin" />正在读取桌面配置…</div>}

        {notImplemented ? <section className="panel construction-panel">
          <span className="construction-icon"><selected.icon size={30} strokeWidth={1.5} /></span>
          <Status>尚未开放</Status><h2>{selected.label}</h2><p>{selected.description}</p>
          <p className="construction-detail">此模块正在开发，当前版本可使用服务连接、账号登录与服务概况。</p>
          <button className="button button-secondary" onClick={() => setPage('overview')}><ArrowLeft size={16} />返回工作台</button>
        </section> : <>
          <div className="setup-grid">{connectionCard()}{page === 'settings' ? <section className="panel settings-panel">
            <div className="panel-heading"><span className="settings-icon"><ShieldCheck size={20} /></span>
              <div><h2>环境与会话</h2><p>当前客户端的连接规则</p></div></div>
            <div className="settings-body">
              <div className="settings-row"><Server size={18} /><div><h3>环境独立</h3><p>开发与正式客户端分别保存配置，只连接匹配的服务环境。</p></div></div>
              <div className="settings-row"><LockKeyhole size={18} /><div><h3>密码不落盘</h3><p>密码仅用于本次登录，不写入本机连接配置。</p></div></div>
              <div className="settings-row"><KeyRound size={18} /><div><h3>系统管理会话</h3><p>登录会话保存在系统凭据存储，退出登录不会停止服务。</p></div></div>
            </div>
            <div className="panel-footnote"><Monitor size={14} /><span>当前环境：{snapshot ? formatEnvironment(snapshot.environment) : '等待桌面客户端识别'}</span></div>
          </section> : loginCard()}</div>

          <section className="panel workspace-panel" aria-labelledby="workspace-title">
            <div className="workspace-heading"><div className="workspace-heading-title"><Server size={18} /><h2 id="workspace-title">服务概况</h2></div>
              {session ? <button className="text-button" disabled={disabled} onClick={() => void perform('refresh', () => nativeCommand('refresh_workspace'))}>
                <RefreshCw size={14} className={busy === 'refresh' ? 'spin' : ''} />{busy === 'refresh' ? '刷新中…' : '刷新概况'}</button>
                : <span className="summary-caption">登录后自动加载</span>}
            </div>
            {workspace && snapshot?.system ? <div className="live-workspace">
              <dl className="service-details">
                <div><dt>服务环境</dt><dd>{formatEnvironment(workspace.environment)}</dd></div>
                <div><dt>服务版本</dt><dd>{snapshot.system.app_version}<span className="detail-secondary">API v{snapshot.system.api_version}</span></dd></div>
                <div><dt>当前账号</dt><dd>{workspace.username}</dd></div>
                <div><dt>数据库</dt><dd className="database-state"><Database size={15} />{workspace.database === 'connected' ? '已连接' : workspace.database}</dd></div>
              </dl>
              <div className="instance-row"><span>实例标识</span><code>{workspace.instance_id}</code></div>
            </div> : <div className="workspace-empty">
              <span className="empty-icon">{session ? <RefreshCw size={23} strokeWidth={1.5} /> : <Unplug size={23} strokeWidth={1.5} />}</span>
              <div><h3>{session ? '服务概况暂未加载' : '等待连接与登录'}</h3>
                <p>{session ? '会话已建立，请刷新概况以获取服务的当前状态。' : '完成后，这里会显示服务环境、版本、账号和数据库状态。'}</p></div>
            </div>}
          </section>
        </>}
        <footer className="page-footer"><span>采购对账工作台</span><span><ShieldCheck size={13} />{native ? '桌面安全连接' : '浏览器界面预览'}</span></footer>
      </div></main>
    </div>
  </div>;
}

export default App;
