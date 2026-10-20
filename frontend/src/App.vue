<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Activity, ArrowDownLeft, ArrowUpRight, Bell, Check, CheckCheck, ChevronDown, ChevronRight, CircleHelp,
  Clock3, Database, ExternalLink, FileText, Globe2, LayoutDashboard, LoaderCircle, LogOut, Menu,
  Monitor, Pause, Play, Radio, RefreshCw, Search, Send, Server, Settings2, ShieldCheck, Signal,
  SlidersHorizontal, Users, X } from 'lucide-vue-next'
import { api, type Settings, type Snapshot, type SessionDetail, type User, type Notification } from './api'

type Page = 'overview' | 'users' | 'sessions' | 'source' | 'notifications' | 'settings'
const page = ref<Page>('overview')
const navigation = [
  {id: 'overview' as Page, label: '活动概览', icon: LayoutDashboard},
  {id: 'users' as Page, label: '客户端', icon: Users},
  {id: 'sessions' as Page, label: '活动记录', icon: Clock3},
  {id: 'source' as Page, label: '节点接入', icon: Database},
  {id: 'notifications' as Page, label: '通知中心', icon: Bell},
  {id: 'settings' as Page, label: '系统设置', icon: Settings2},
]
const loading = ref(true), busy = ref(false), authenticated = ref(false), initialized = ref(false)
const username = ref(''), password = ref(''), authError = ref(''), search = ref(''), filter = ref('all')
const data = ref<Snapshot | null>(null), form = ref<Settings | null>(null)
const formBase = ref<Settings | null>(null), resumeOpen = ref(false)
const sourceCheck = ref<{ok:boolean; message:string; checks?:Record<string,boolean>} | null>(null)
const toast = ref(''), toastError = ref(false), mobileMenu = ref(false)
const connectionLost = ref(false)
const selected = ref<SessionDetail | null>(null), detailLoading = ref(false), notification = ref<Notification | null>(null)
const editUser = ref<User | null>(null), alias = ref(''), monitored = ref(true)
const accountUsername = ref(''), accountBase = ref(''), currentPassword = ref(''), newPassword = ref(''), confirmPassword = ref(''), helpOpen = ref(false)
const previewKind = ref<'online' | 'offline'>('offline')
let timer: ReturnType<typeof setInterval> | undefined, toastTimer: ReturnType<typeof setTimeout> | undefined

const title = computed(() => navigation.find(n => n.id === page.value)?.label || '')
const notificationLabels: Record<string, string> = {preview: '仅预览', pending: '待发送', sent: '已发送', retry: '等待重试', failed: '发送失败', skipped: '已忽略'}
const stateLabels: Record<string, string> = {online: '在线', pending: '等待下线确认', offline: '离线', unknown: '状态未知'}
const rows = computed(() => (data.value?.users || []).filter(u => {
  const q = search.value.toLowerCase()
  return (u.email.toLowerCase().includes(q) || (u.alias || '').toLowerCase().includes(q)) && (filter.value === 'all' || u.state === filter.value)
}))
const sessionRows = computed(() => (data.value?.sessions || []).filter(s => [s.email, s.alias, s.last_ip].some(v => (v || '').toLowerCase().includes(search.value.toLowerCase()))))
const latestNotifications = computed(() => (data.value?.notifications || []).slice(0, 3))
const preview = computed(() => {
  const messages = data.value?.notifications || []
  return messages.find(m => m.kind === previewKind.value)?.text || (previewKind.value === 'online' ? '🟢 用户 上线\n\n🌐 IP：暂无记录\n🚪 入站：暂无记录\n🎯 首次访问：暂无记录\n🕐 时间：首次检测上线时间' : '🔴 用户 下线\n\n完成一轮活动后，这里将展示真实汇总。\n\n💤 已连续离线 5 分钟')
})
function settingChanges(): Record<string,unknown> {
  if (!form.value || !formBase.value) return {}
  const current = form.value as unknown as Record<string,unknown>
  const original = formBase.value as unknown as Record<string,unknown>
  return Object.fromEntries(Object.keys(current).filter(key =>
    !['monitor_enabled','delivery_paused','settings_revision'].includes(key) && !key.endsWith('_configured') &&
    current[key] !== original[key]).map(key => [key,current[key]]))
}
const formDirty = computed(() => Object.keys(settingChanges()).length > 0)
const statusLabels: Record<string,string> = {ok:'正常',idle:'等待采集',remote:'远程接口',paused:'已暂停',disabled:'未启用',stopped:'监控暂停',queued:'等待发送',error:'异常',catching_up:'追赶中'}
const healthCards = computed(() => {
  if (!data.value) return []
  const d=data.value
  return [
    {label:'客户端同步',status:d.health.inventory,at:d.health.last_inventory,detail:d.settings.source_type==='local' ? `${d.users.filter(u=>u.source_present).length} 个本机客户端` : '远程模式按在线列表识别'},
    {label:'在线状态查询',status:d.health.panel,at:d.health.last_poll,detail:d.health.detail},
    {label:'访问日志读取',status:d.health.log,at:d.health.last_log_check,detail:d.health.log_detail},
    {label:'Telegram 推送',status:d.delivery.status,at:d.delivery.last_success,detail:d.delivery.last_error || `${d.delivery.pending} 条待发 · ${d.delivery.failed} 条失败`},
  ]
})
const maxBar = computed(() => Math.max(1, ...(data.value?.histogram || []).map(b => b.count)))
const clock = (at?: number | null, date = false) => at ? new Intl.DateTimeFormat('zh-CN', {timeZone: data.value?.settings.timezone || 'Asia/Shanghai', ...(date ? {month:'2-digit', day:'2-digit'} : {}), hour: '2-digit', minute:'2-digit', ...(date ? {} : {second:'2-digit'}), hour12:false}).format(at * 1000) : '—'
const duration = (start?: number | null, end?: number | null) => {
  if (!start) return '—'
  const sec = Math.max(0, Math.floor((end || data.value?.now || Date.now() / 1000) - start))
  return sec >= 3600 ? `${Math.floor(sec / 3600)} 小时 ${Math.floor(sec % 3600 / 60)} 分` : `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`
}
const countdown = (since?: number | null) => {
  if (!since || !data.value) return ''
  return Math.max(0, Math.ceil((data.value.settings.offline_seconds - (data.value.now - since)) / 60)) + ' 分钟内确认'
}
function notify(message: string, error = false) {
  toast.value = message; toastError.value = error
  clearTimeout(toastTimer); toastTimer = setTimeout(() => toast.value = '', 5000)
}
async function action(task: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  try { await task() } catch (e) { notify((e as Error).message, true) } finally { busy.value = false }
}
async function refresh(resetForm = false) {
  try {
    const result = await api<Snapshot>('/snapshot')
    data.value = result
    if (resetForm || accountUsername.value === accountBase.value) accountUsername.value = result.admin_username
    accountBase.value = result.admin_username
    connectionLost.value = false
    if (resetForm || !form.value || !formDirty.value) {
      form.value = {...result.settings, panel_password: '', panel_totp: '', telegram_token: ''}
      formBase.value = {...form.value}
    }
  } catch (e) {
    connectionLost.value = true
    throw e
  }
}
function navigate(id: Page) { page.value = id; search.value = ''; filter.value = 'all'; mobileMenu.value = false }
async function authenticate() {
  busy.value = true; authError.value = ''
  try {
    await api(initialized.value ? '/auth/login' : '/auth/setup', 'POST', {username: username.value.trim(), password: password.value})
    password.value = ''; authenticated.value = true; initialized.value = true
    await refresh(true)
  } catch (e) { authError.value = (e as Error).message } finally { busy.value = false }
}
async function logout() {
  await action(async () => {await api('/auth/logout', 'POST'); authenticated.value = false; data.value = null; form.value = null})
}
async function save() {
  await action(async () => {
    if (!form.value) return
    const body = {...settingChanges(),settings_revision:formBase.value?.settings_revision}
    if (page.value==='source') {
      const result=await api<{ok:boolean;message:string;saved:boolean;checks:Record<string,boolean>}>('/settings/check-save','POST',body)
      sourceCheck.value=result
      if (!result.saved) {notify(result.message,true);return}
      notify('检查通过，配置已保存；监控状态保持不变')
    } else {
      await api('/settings','PATCH',body); notify('设置已保存，即时生效')
    }
    await refresh(true)
  })
}
async function toggleMonitor() {
  await action(async () => {
    const enabled = !data.value?.settings.monitor_enabled
    const edits=settingChanges()
    await api('/monitor', 'POST', {enabled}); await refresh(true); Object.assign(form.value!,edits)
    notify(enabled ? '监控已启动' : '监控已暂停')
  })
}
async function checkSource() {
  await action(async () => {
    const result = await api<{ok: boolean; message: string;checks:Record<string,boolean>}>('/source/check', 'POST', {...settingChanges(),settings_revision:formBase.value?.settings_revision})
    sourceCheck.value=result; notify(result.message, !result.ok)
  })
}
async function detectSource() {
  await action(async()=>{
    const result=await api<{ok:boolean;message:string;detected?:Partial<Settings>}>('/source/detect','POST',{})
    if (result.ok && result.detected && form.value) Object.assign(form.value,result.detected)
    notify(result.message,!result.ok)
  })
}
async function switchDelivery(backlog:'keep'|'discard'='keep') {
  await action(async()=>{
    const edits=settingChanges()
    const paused=!data.value?.settings.delivery_paused
    await api('/delivery','POST',{paused,backlog})
    resumeOpen.value=false
    await refresh(true); Object.assign(form.value!,edits)
    notify(paused?'推送已暂停，待发提醒会保留':backlog==='discard'?'已忽略积压并恢复推送':'已恢复推送，保留原有队列')
  })
}
function toggleDelivery() {
  if (data.value?.settings.delivery_paused && data.value.delivery.pending+data.value.delivery.failed>0) resumeOpen.value=true
  else switchDelivery()
}
async function openSession(id: number | null) {
  if (!id) return
  detailLoading.value = true
  try { selected.value = await api<SessionDetail>(`/sessions/${id}`) } catch(e) { notify((e as Error).message, true) }
  finally { detailLoading.value = false }
}
function openEdit(user: User) { editUser.value = user; alias.value = user.alias; monitored.value = !!user.monitored }
async function saveUser() {
  await action(async () => {
    if (!editUser.value) return
    await api(`/users/${encodeURIComponent(editUser.value.email)}`, 'PUT', {alias: alias.value, monitored: monitored.value})
    editUser.value = null; await refresh(); notify('客户端设置已保存')
  })
}
async function retry(id: number) {
  await action(async () => { await api(`/notifications/${id}/retry`, 'POST'); await refresh(); notify('已加入重试队列') })
}
async function testTelegram() {
  await action(async () => { const result = await api('/telegram/test', 'POST'); notify(result.message || '测试完成') })
}
async function changeAccount() {
  await action(async () => {
    if (newPassword.value !== confirmPassword.value) throw new Error('两次新密码不同，请重新输入')
    const result = await api<{changed:boolean}>('/auth/account', 'PUT', {username: accountUsername.value.trim(), current_password: currentPassword.value, new_password: newPassword.value})
    currentPassword.value = ''; newPassword.value = ''; confirmPassword.value = ''
    await refresh(true)
    notify(result.changed ? '管理员账号已更新，其他登录会话已失效' : '账号信息没有变化')
  })
}
function expired() { authenticated.value = false; data.value = null; form.value = null; notify('登录已过期，请重新登录', true) }
function closeOnEscape(event: KeyboardEvent) {
  if (event.key === 'Escape') {selected.value=null;notification.value=null;editUser.value=null;helpOpen.value=false;resumeOpen.value=false;mobileMenu.value=false}
}
onMounted(async () => {
  window.addEventListener('session-expired', expired)
  window.addEventListener('keydown', closeOnEscape)
  try {
    const status = await api<{initialized: boolean; authenticated: boolean}>('/auth/status')
    initialized.value = status.initialized; authenticated.value = status.authenticated
    if (status.authenticated) await refresh(true)
  } catch(e) { authError.value = (e as Error).message }
  finally { loading.value = false }
  timer = setInterval(() => {if (authenticated.value) refresh().catch(() => {})}, 10000)
})
onUnmounted(() => {clearInterval(timer); clearTimeout(toastTimer); window.removeEventListener('session-expired', expired);window.removeEventListener('keydown',closeOnEscape)})
</script>

<template>
  <div v-if="loading" class="boot"><LoaderCircle class="spin" :size="24"/><span>正在打开 Session Watch</span></div>
  <main v-else-if="!authenticated" class="auth-layout">
    <section class="auth-story">
      <div class="brand"><span class="brand-mark"><Activity :size="23"/></span><span>Session<span class="brand-light">Watch</span></span></div>
      <div class="story-content"><span class="eyebrow">A QUIETER WAY TO STAY INFORMED</span><h1>连接之间，<br/>尽在掌握<span>。</span></h1><p>每一次上线，及时知晓。<br/>每一段活动，清晰回顾。</p><div class="story-line"><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span></div></div>
      <div class="story-footer"><ShieldCheck :size="16"/> 独立运行 · 只读访问 · 自主掌控</div>
    </section>
    <section class="auth-panel"><div class="auth-card"><span class="eyebrow">YOUR PRIVATE OBSERVATORY</span><h2>{{ initialized ? '欢迎回来' : '创建你的管理账号' }}</h2><p>{{ initialized ? '登录以查看客户端活动与通知。' : '首次使用，先设置一个独立于 3x-ui 的管理员账号。' }}</p>
      <form @submit.prevent="authenticate"><label>管理员账号<input v-model="username" autocomplete="username" required maxlength="80" placeholder="输入管理员账号"/></label><label>密码<input v-model="password" type="password" :autocomplete="initialized ? 'current-password' : 'new-password'" required minlength="12" maxlength="256" placeholder="至少 12 个字符"/></label><div v-if="authError" class="error-box">{{ authError }}</div><button class="button primary full" :disabled="busy"><LoaderCircle v-if="busy" class="spin" :size="16"/>{{ initialized ? '登录控制台' : '创建账号并进入' }}<ArrowUpRight :size="17"/></button></form>
      <div class="auth-note"><ShieldCheck :size="16"/><span>独立管理账号；同机部署可免密读取 3x-ui 客户端与在线状态。</span></div></div><span class="auth-copyright">SESSION WATCH / v0.5.0</span></section>
  </main>
  <div v-else-if="data && form" class="app-shell">
    <div v-if="mobileMenu" class="sidebar-scrim" @click="mobileMenu=false"></div>
    <aside class="sidebar" :class="{open: mobileMenu}">
      <a class="brand" href="#" @click.prevent="navigate('overview')"><span class="brand-mark"><Activity :size="23"/></span><span>Session<span class="brand-light">Watch</span></span></a>
      <div class="workspace"><span class="workspace-icon"><Server :size="17"/></span><div><strong>我的工作空间</strong><span>客户端活动监控</span></div><ChevronDown :size="15"/></div>
      <div class="nav-caption">工作台</div>
      <nav aria-label="主导航"><button v-for="item in navigation" :key="item.id" :class="{active: page===item.id}" @click="navigate(item.id)"><component :is="item.icon" :size="18"/><span>{{ item.label }}</span><span v-if="item.id==='users'" class="nav-count">{{ data.users.length }}</span><span v-if="item.id==='notifications' && data.totals.failures" class="nav-alert">{{ data.totals.failures }}</span></button></nav>
      <div class="sidebar-bottom"><div class="boundary-card"><ShieldCheck :size="20"/><strong>轻量观察，独立运行</strong><p>仅查询状态、读取日志。<br/>不修改你的 3x-ui 配置。</p><button @click="helpOpen=true">了解工作方式 <ArrowUpRight :size="13"/></button></div><button class="profile" @click="logout" title="退出登录"><span class="avatar admin">A</span><div><strong>管理员</strong><span>本地工作空间</span></div><LogOut :size="17"/></button><div class="version"><span class="tiny-dot"></span> Session Watch <span>v{{ data.version }}</span></div></div>
    </aside>
    <section class="main-shell">
      <header class="topbar"><div class="breadcrumbs"><button class="icon-button mobile-toggle" @click="mobileMenu=true" aria-label="打开菜单"><Menu :size="20"/></button><span>工作台</span><ChevronRight :size="14"/><strong>{{ title }}</strong></div><div class="topbar-right"><span class="mode-badge live"><span class="tiny-dot"></span>正式运行</span><button class="icon-button" @click="helpOpen=true" aria-label="帮助"><CircleHelp :size="18"/></button><span class="avatar small">A</span></div></header>
      <div class="page-content">
        <div class="page-heading"><div><div class="eyebrow">{{ page === 'overview' ? 'THE CONNECTION PULSE' : 'SESSION WATCH / CONTROL CENTER' }}</div><h1>{{ title }}<span v-if="page==='overview'" class="heading-dot">.</span></h1><p>{{ {overview:'关注连接的开始，也看清每一次结束。',users:'为每个账号设置名称，跟踪它的每一轮活动。',sessions:'从上线到离线，回顾完整的连接轨迹。',source:'连接状态决定通知，访问日志补充细节。',notifications:'让每一次重要的状态变化，都有回音。',settings:'让观察的节奏，适合你的工作方式。'}[page] }}</p></div>
          <div class="heading-actions"><span v-if="formDirty" class="unsaved-label">有未保存的修改</span><button v-if="formDirty" class="button" :disabled="busy" @click="action(async()=>{await refresh(true);notify('已载入服务器当前配置')})">重新载入</button><button v-if="page==='overview'" class="button" :disabled="busy" @click="action(async()=>{await refresh();notify('数据已刷新')})"><RefreshCw :size="15"/>刷新</button><button v-if="page==='overview'" class="button primary" @click="navigate('source')"><SlidersHorizontal :size="15"/>配置监控</button><button v-if="['source','notifications','settings'].includes(page)" class="button primary" :disabled="busy" @click="save"><Check :size="16"/>{{ page==='source'?'检查并保存':'保存设置' }}</button></div>
        </div>
        <div class="control-bar"><div><span class="status-dot" :class="data.settings.monitor_enabled?'online':'pending'"></span><strong>{{ data.settings.monitor_enabled?'监控运行中':'监控已暂停' }}</strong><span class="muted small-text">{{ data.settings.delivery_paused?'推送已暂停':data.settings.telegram_enabled?'Telegram 已启用':'Telegram 未启用' }}</span></div><div><button class="button" :disabled="busy" @click="toggleMonitor"><Pause v-if="data.settings.monitor_enabled" :size="14"/><Play v-else :size="14"/>{{ data.settings.monitor_enabled?'暂停监控':'启动监控' }}</button><button class="button" :disabled="busy||!data.settings.telegram_enabled" @click="toggleDelivery"><Bell :size="14"/>{{ data.settings.delivery_paused?'恢复推送':'暂停推送' }}</button></div></div>
        <div v-if="data.health.panel==='error'" class="error-box">{{ data.health.detail }}。查询异常不会触发下线通知。</div>
        <div v-if="connectionLost" class="error-box">页面数据刷新失败，当前显示上一次快照。正在尝试重新连接。</div>

        <template v-if="page==='overview'">
          <div class="health-strip"><article v-for="card in healthCards" :key="card.label" class="health-card"><div><strong>{{ card.label }}</strong><span class="status-pill" :class="card.status==='ok'?'online':card.status==='error'?'error':'unknown'">{{ statusLabels[card.status]||card.status }}</span></div><p>{{ card.detail }}</p><small>最近成功：{{ clock(card.at,true) }}</small></article></div>
          <div class="metrics-grid">
            <article class="metric-card"><div class="metric-label">当前在线<Radio :size="17"/></div><div class="metric-value">{{ data.totals.online }}<span>个客户端</span></div><div class="metric-footer"><span class="status-dot online"></span>来自在线状态查询</div></article>
            <article class="metric-card"><div class="metric-label">等待下线确认<Clock3 :size="17"/></div><div class="metric-value">{{ data.totals.pending }}<span>个客户端</span></div><div class="metric-footer"><span class="status-dot pending"></span>连续离线 {{ data.settings.offline_seconds / 60 }} 分钟后汇总</div></article>
            <article class="metric-card"><div class="metric-label">今日连接记录<Activity :size="17"/></div><div class="metric-value">{{ data.totals.requests.toLocaleString() }}<span>条记录</span></div><div class="metric-footer">{{ data.totals.sessions }} 轮活动中的访问日志</div></article>
            <article class="metric-card"><div class="metric-label">推送异常<Bell :size="17"/></div><div class="metric-value">{{ data.totals.failures }}<span>条消息</span></div><div class="metric-footer"><span class="status-dot" :class="data.totals.failures ? 'pending' : 'online'"></span>{{ data.totals.failures ? '请前往通知中心处理' : '暂无失败或待重试消息' }}</div></article>
          </div>
          <div class="overview-columns">
            <article class="panel activity-chart"><div class="panel-header"><div><h2>今日活动节奏</h2><p>按首次检测上线时间统计活动轮数</p></div><span class="subtle-badge">{{ clock(data.now, true).split(' ')[0] }} · {{ data.settings.timezone }}</span></div><div class="chart-area"><div class="chart-labels"><span>{{ maxBar }}</span><span>{{ maxBar > 1 ? Math.ceil(maxBar/2) : "" }}</span><span>0</span></div><div class="chart-grid"><div class="gridline top"></div><div class="gridline mid"></div><div class="gridline bottom"></div><div v-for="bar in data.histogram" :key="bar.hour" class="bar-column" :title="`${bar.hour}:00 · ${bar.count} 轮活动`"><div class="bar" :class="{populated:bar.count>0}" :style="{height: `${bar.count ? Math.max(8, bar.count/maxBar*100) : 2}%`}"></div><span v-if="bar.hour%4===0">{{ String(bar.hour).padStart(2,'0') }}:00</span></div></div></div><div class="chart-caption"><span><i></i>活动开始</span><span>每天 {{ data.settings.timezone==='Asia/Shanghai' ? '北京时间' : data.settings.timezone }} 00:00 起统计</span></div></article>
            <article class="panel rule-card"><div class="rule-icon"><ShieldCheck :size="23"/></div><span class="eyebrow">A LITTLE PATIENCE, LESS NOISE</span><h2>稍等五分钟，<br/>让通知更从容。</h2><p>短暂断开后恢复在线，会继续同一轮活动，不重复打扰。</p><div class="rule-flow"><span class="flow-dot"></span>检测离线<span class="flow-dashes"></span><strong>{{ data.settings.offline_seconds/60 }} min</strong><span class="flow-dashes"></span><Send :size="15"/></div><button class="text-button" @click="navigate('notifications')">管理通知规则 <ChevronRight :size="15"/></button></article>
          </div>
          <article class="panel clients-panel"><div class="panel-header"><div><h2>客户端动态 <span class="count-label">{{ data.users.length }}</span></h2><p>在线状态与访问活动，一目了然</p></div><button class="text-button" @click="navigate('users')">全部客户端 <ArrowUpRight :size="15"/></button></div><div class="table-wrap"><table><thead><tr><th>客户端</th><th>当前状态</th><th>来源 IP</th><th>连接记录</th><th>活动时长</th><th></th></tr></thead><tbody><tr v-for="(user,i) in data.users.slice(0,5)" :key="user.email"><td><div class="user-cell"><span class="avatar" :class="'color-'+i%4">{{ (user.alias||user.email).slice(0,1).toUpperCase() }}</span><div><strong>{{ user.alias || user.email }}</strong><span>{{ user.email }}</span><span v-if="user.source_present!=null" class="cell-note">{{ !user.source_present ? '已从 3x-ui 移除 · 保留历史' : user.source_enabled ? '3x-ui 已启用' : '3x-ui 已禁用' }}<template v-if="user.source_inbounds"> · {{ JSON.parse(user.source_inbounds).join('、') || '未关联入站' }}</template></span></div></div></td><td><span class="status-pill" :class="user.state||'unknown'"><span class="tiny-dot"></span>{{ stateLabels[user.state||'unknown'] }}</span></td><td class="mono muted">{{ user.last_ip || '—' }}</td><td>{{ user.requests || 0 }}<span class="unit">条</span></td><td class="muted">{{ ['online','pending'].includes(user.state||'') ? duration(user.started_at,user.offline_since) : '—' }}</td><td><button class="icon-button" @click="openSession(user.session_id)" aria-label="查看活动详情"><ChevronRight :size="17"/></button></td></tr><tr v-if="!data.users.length"><td colspan="6" class="empty">暂无客户端。启用监控后将从在线状态和日志中发现用户。</td></tr></tbody></table></div></article>
          <div class="overview-bottom"><article class="panel"><div class="panel-header"><h2>主要访问目标</h2><Globe2 :size="17" class="muted"/></div><div class="target-list"><div v-for="(target,i) in data.targets" :key="target.value" class="target-row"><span class="rank">{{ String(i+1).padStart(2,'0') }}</span><div><span>{{ target.value }}</span><div class="target-track"><i :style="{width:`${target.count / (data.targets[0]?.count||1)*100}%`}"></i></div></div><strong>{{ target.count }}<span>次</span></strong></div><div v-if="!data.targets.length" class="empty">还没有今日访问记录</div></div></article><article class="panel"><div class="panel-header"><h2>最近通知</h2><button class="text-button" @click="navigate('notifications')">查看全部 <ChevronRight :size="14"/></button></div><div class="recent-notifications"><button v-for="item in latestNotifications" :key="item.id" class="recent-notification" @click="notification=item"><span class="event-icon" :class="item.kind"><ArrowUpRight v-if="item.kind==='online'" :size="17"/><ArrowDownLeft v-else :size="17"/></span><div><strong>{{ item.alias||item.email }} {{ item.kind==='online' ? '上线' : '下线汇总' }}</strong><span>{{ notificationLabels[item.status] }} · {{ clock(item.created_at) }}</span></div><ChevronRight :size="15"/></button><div v-if="!latestNotifications.length" class="empty">通知将在状态变化后出现</div></div><div class="panel-footnote"><ShieldCheck :size="13"/>所有发送均由后台任务完成</div></article></div>
        </template>

        <template v-if="page==='users'">
          <article class="panel"><div class="table-toolbar"><div class="search-input"><Search :size="17"/><input v-model="search" placeholder="搜索名称或 email" aria-label="搜索客户端"/></div><select v-model="filter" aria-label="按状态筛选"><option value="all">所有状态</option><option value="online">在线</option><option value="pending">等待下线确认</option><option value="offline">离线</option><option value="unknown">状态未知</option></select><span class="muted small-text">共 {{ rows.length }} 个客户端</span></div><div class="table-wrap"><table><thead><tr><th>客户端</th><th>状态</th><th>来源 IP</th><th>连接 / 目标</th><th>监控</th><th>管理</th></tr></thead><tbody><tr v-for="(user,i) in rows" :key="user.email"><td><div class="user-cell"><span class="avatar" :class="'color-'+i%4">{{ (user.alias||user.email).slice(0,1) }}</span><div><strong>{{ user.alias||user.email }}</strong><span>{{ user.email }}</span><span v-if="user.source_present!=null" class="cell-note">{{ !user.source_present ? '已从 3x-ui 移除 · 保留历史' : user.source_enabled ? '3x-ui 已启用' : '3x-ui 已禁用' }}<template v-if="user.source_inbounds"> · {{ JSON.parse(user.source_inbounds).join('、') || '未关联入站' }}</template></span></div></div></td><td><span class="status-pill" :class="user.state||'unknown'"><span class="tiny-dot"></span>{{ stateLabels[user.state||'unknown'] }}</span><span v-if="user.state==='pending'" class="cell-note">{{ countdown(user.offline_since) }}</span></td><td class="mono muted">{{ user.last_ip||'—' }}</td><td>{{ user.requests||0 }} / {{ user.targets||0 }}</td><td><span :class="user.monitored ? 'text-green' : 'muted'">{{ user.monitored ? '已启用' : '已暂停' }}</span></td><td><div class="inline-actions"><button class="text-button" @click="openEdit(user)">编辑</button><button class="text-button" @click="openSession(user.session_id)">详情</button></div></td></tr><tr v-if="!rows.length"><td colspan="6" class="empty">没有匹配的客户端</td></tr></tbody></table></div><div class="panel-footnote">同一 email 代表一个账号；多个设备共用账号时，活动合并统计。暂停用户监控会结束当前统计，不发送下线提醒。</div></article>
        </template>

        <template v-if="page==='sessions'">
          <article class="panel"><div class="table-toolbar"><div class="search-input"><Search :size="17"/><input v-model="search" placeholder="搜索客户端或 IP" aria-label="搜索活动"/></div><span class="muted small-text">最近 100 轮活动 · 点击查看汇总</span></div><div class="table-wrap"><table><thead><tr><th>客户端</th><th>状态</th><th>开始时间</th><th>时长</th><th>连接记录</th><th>访问目标</th><th></th></tr></thead><tbody><tr v-for="session in sessionRows" :key="session.id"><td><strong>{{ session.alias||session.email }}</strong><span class="cell-note">{{ session.last_ip||'暂无 IP' }}</span></td><td><span class="status-pill" :class="session.state"><span class="tiny-dot"></span>{{ stateLabels[session.state] }}</span></td><td class="muted">{{ clock(session.started_at,true) }}</td><td>{{ duration(session.started_at,session.ended_at||session.offline_since) }}</td><td>{{ session.requests }}</td><td>{{ session.targets }}</td><td><button class="icon-button" @click="openSession(session.id)" aria-label="查看活动详情"><ChevronRight :size="17"/></button></td></tr><tr v-if="!sessionRows.length"><td colspan="7" class="empty">还没有活动记录</td></tr></tbody></table></div></article>
        </template>

        <template v-if="page==='source'">
          <div class="notice-bar"><ShieldCheck :size="16"/>{{ data.settings.monitor_enabled?'当前配置继续监控。新接入参数检查通过后生效，失败则保留原配置。':'检查通过并保存后，点击启动监控。普通设置可直接保存生效。' }}</div><div v-if="sourceCheck" class="source-result" :class="{bad:!sourceCheck.ok}"><strong>上次检查：{{ sourceCheck.ok?'通过':'未通过' }}</strong><p>{{ sourceCheck.message }}</p><div v-if="sourceCheck.checks"><span v-for="(ok,key) in sourceCheck.checks" :key="key">{{ {inventory:'客户端来源',online:'在线查询',log:'日志读取'}[key]||key }}：{{ ok?'通过':'失败' }}</span></div></div>
          <div class="settings-layout"><div class="settings-stack"><article class="panel form-panel"><div class="section-heading"><span class="section-icon"><Server :size="19"/></span><div><h2>状态来源</h2><p>自动同步本机客户端清单，查询现有 Xray 在线状态</p></div></div><fieldset><button class="button detect-button" :disabled="busy" @click="detectSource"><Search :size="15"/>自动检测本机</button><label>接入方式<select v-model="form.source_type"><option value="local">本机自动读取 · 无需 3x-ui 账号密码</option><option value="panel">远程面板 API · 使用账号密码</option></select></label><label>节点名称<input v-model="form.source_name" placeholder="例如：六六云" maxlength="80"/></label><template v-if="form.source_type==='local'"><div class="notice-bar"><ShieldCheck :size="16"/>独立采集器只读获取全部客户端，不读取面板登录凭据或导出代理密钥。</div><label>本地采集快照<input v-model="form.local_snapshot_path" class="mono"/><small>由本机采集服务自动更新；默认路径无需修改。</small></label></template><template v-else><label>面板根地址<input v-model="form.panel_url" type="url" placeholder="https://panel.example.com/你的面板路径"/><small>包含自定义面板路径，不要填写 /panel/api。HTTPS 证书会正常校验。</small></label><div class="form-row"><label>面板账号<input v-model="form.panel_username" autocomplete="off" placeholder="输入面板账号"/></label><label>面板密码<input v-model="form.panel_password" type="password" autocomplete="new-password" :placeholder="form.panel_password_configured ? '已安全保存，留空保持不变' : '输入面板密码'"/></label></div><div class="form-row"><label>接口版本<select v-model="form.online_api"><option value="auto">自动探测（新版 → 旧版）</option><option value="clients">新版 clients/onlines</option><option value="inbounds">旧版 inbounds/onlines</option></select></label><label>二步验证码（可选）<input v-model="form.panel_totp" autocomplete="off" :placeholder="form.panel_totp_configured ? '已保存；重登录时需更新' : '仅在面板启用 2FA 时填写'"/></label></div></template></fieldset></article>
          <article class="panel form-panel"><div class="section-heading"><span class="section-icon"><FileText :size="19"/></span><div><h2>访问日志</h2><p>只读提取 IP、入站及访问目标，不写入源文件</p></div></div><fieldset><label>日志文件路径<input v-model="form.log_path" class="mono" placeholder="/usr/local/x-ui/access.log"/><small>指本应用所在机器或容器内的路径。首次接入从文件末尾开始，不补发历史活动。</small></label><label>日志时间所在时区<input v-model="form.log_timezone" placeholder="Etc/UTC 或 Asia/Shanghai"/><small>无时区的日志按此解析；消息显示时区在系统设置中单独配置。</small></label></fieldset><div class="form-actions"><span class="muted small-text">检查当前输入；检查本身不会保存</span><button class="button" :disabled="busy" @click="checkSource"><RefreshCw :size="15"/>仅检查当前输入</button></div></article></div>
          <aside class="settings-aside"><article class="panel form-panel"><h2>采集状态</h2><div class="health-row"><span>在线状态查询</span><span class="status-pill" :class="data.health.panel==='ok'?'online':'unknown'">{{ {ok:'正常',idle:'未连接',paused:'已暂停',error:'异常'}[data.health.panel]||data.health.panel }}</span></div><div class="health-row"><span>日志读取</span><span class="status-pill" :class="data.health.log==='ok'?'online':'unknown'">{{ {ok:'正常',idle:'未连接',paused:'已暂停',error:'异常',catching_up:'追赶中'}[data.health.log]||data.health.log }}</span></div><div class="health-row"><span>最近查询</span><span>{{ clock(data.health.last_poll) }}</span></div><div class="health-row"><span>本次运行解析</span><span>{{ data.health.parsed }} 条</span></div><p class="muted small-text">{{ data.health.detail }}</p><button class="button full" :class="{'primary':!data.settings.monitor_enabled}" :disabled="busy" @click="toggleMonitor"><Pause v-if="data.settings.monitor_enabled" :size="15"/><Play v-else :size="15"/>{{ data.settings.monitor_enabled?'暂停监控':'启动监控' }}</button></article><article class="aside-note"><ShieldCheck :size="21"/><h3>保持只读边界</h3><p>本机接入只读数据库并查询已有 Xray 接口；远程接入通过面板 API 查询。不修改入站、客户端或流量。</p><p>访问日志的开启与轮转，由服务器管理员管理。</p></article></aside></div>
        </template>

        <template v-if="page==='notifications'">
          <div class="settings-layout"><div class="settings-stack"><article class="panel form-panel"><div class="section-heading"><span class="section-icon"><Send :size="19"/></span><div><h2>Telegram 推送</h2><p>每轮活动各发送一条上线提醒和下线汇总</p></div></div><fieldset><label class="switch-label"><span><strong>启用实际发送</strong><small>关闭时仅在通知记录中生成预览，不会补发这些预览。</small></span><input v-model="form.telegram_enabled" type="checkbox" class="switch"/></label><label class="switch-label"><span><strong>推送 IP 归属地</strong><small>通过 IP9（HTTPS）查询公网 IP，不上传账号或访问目标。定位以数据源为准，不保证县区准确度。</small></span><input v-model="form.ip_location_enabled" type="checkbox" class="switch"/></label><label>Bot Token<input v-model="form.telegram_token" type="password" autocomplete="new-password" :placeholder="form.telegram_token_configured ? 'Token 已加密保存，留空保持不变' : '从 @BotFather 获取机器人 Token'"/></label><label>Chat ID<input v-model="form.telegram_chat_id" placeholder="个人或群组 Chat ID" autocomplete="off"/></label></fieldset><div class="form-actions"><span class="muted small-text">测试使用已保存配置，暂停推送时也可手动测试</span><button class="button" :disabled="busy||!data.settings.telegram_enabled" @click="testTelegram"><Send :size="15"/>发送测试消息</button></div></article>
          <article class="panel form-panel"><div class="section-heading"><span class="section-icon"><Clock3 :size="19"/></span><div><h2>通知规则</h2><p>按在线状态判断离线，按访问日志生成汇总</p></div></div><fieldset><div class="form-row"><label>连续离线确认<select v-model.number="form.offline_seconds"><option :value="300">5 分钟（默认）</option><option :value="600">10 分钟</option><option :value="900">15 分钟</option><option :value="1800">30 分钟</option><option :value="3600">60 分钟</option></select></label><label>目标排行数量<input v-model.number="form.top_targets" type="number" min="1" max="50"/></label></div></fieldset><ul class="rule-list"><li><Check :size="15"/>等待期间恢复在线，继续同一轮统计，不重复上线提醒</li><li><Check :size="15"/>状态接口异常记为未知，重新开始离线确认</li><li><Check :size="15"/>在线时长不包含最后的离线确认等待时间</li></ul></article></div>
          <aside class="preview-panel"><div class="preview-heading"><span>消息预览</span><div class="segmented"><button :class="{selected:previewKind==='online'}" @click="previewKind='online'">上线</button><button :class="{selected:previewKind==='offline'}" @click="previewKind='offline'">下线</button></div></div><div class="telegram-preview"><div class="telegram-bot"><span class="bot-avatar"><Activity :size="18"/></span><div><strong>Session Watch</strong><span>机器人</span></div></div><div class="message-bubble"><pre>{{ preview }}</pre><span class="message-time">{{ clock(data.now).slice(0,5) }} <CheckCheck :size="13"/></span></div><span class="preview-note">最近活动预览 · 发送结果见通知记录</span></div></aside></div>
          <article class="panel notification-history"><div class="panel-header"><h2>通知记录</h2><span class="muted small-text">最近 100 条</span></div><div class="table-wrap"><table><thead><tr><th>事件</th><th>客户端</th><th>生成时间</th><th>发送状态</th><th>操作</th></tr></thead><tbody><tr v-for="item in data.notifications" :key="item.id"><td><span class="event-label"><span class="event-icon compact" :class="item.kind"><ArrowUpRight v-if="item.kind==='online'" :size="15"/><ArrowDownLeft v-else :size="15"/></span>{{ item.kind==='online'?'上线提醒':'下线汇总' }}</span></td><td>{{ item.alias||item.email }}</td><td class="muted">{{ clock(item.created_at,true) }}</td><td><span class="status-pill" :class="item.status==='sent'?'online':item.status==='failed'?'error':'unknown'">{{ notificationLabels[item.status] }}</span><span v-if="item.error" class="cell-note error-text">{{ item.error }}</span></td><td><div class="inline-actions"><button class="text-button" @click="notification=item">查看</button><button v-if="['failed','retry'].includes(item.status)" class="text-button" :disabled="busy" @click="retry(item.id)">重试</button></div></td></tr><tr v-if="!data.notifications.length"><td colspan="5" class="empty">暂无通知</td></tr></tbody></table></div></article>
        </template>

        <template v-if="page==='settings'">
          <div class="settings-layout"><div class="settings-stack"><article class="panel form-panel"><div class="section-heading"><span class="section-icon"><SlidersHorizontal :size="19"/></span><div><h2>运行设置</h2><p>普通设置保存后即时生效</p></div></div><fieldset><div class="form-row"><label>状态查询间隔（秒）<input v-model.number="form.poll_seconds" type="number" min="5" max="60"/></label><label>活动保留时间（天）<input v-model.number="form.retention_days" type="number" min="1" max="365"/></label></div></fieldset><label>页面与通知显示时区<input v-model="form.timezone" placeholder="Asia/Shanghai"/></label><p class="muted small-text">只清理本项目的过期活动统计，不删除源访问日志。未发送消息对应的活动会保留。</p></article><article class="panel form-panel"><div class="section-heading"><span class="section-icon"><ShieldCheck :size="19"/></span><div><h2>管理员账号</h2><p>验证当前密码后修改；其他登录会话将失效</p></div></div><form @submit.prevent="changeAccount"><label>管理员用户名<input v-model="accountUsername" autocomplete="username" required maxlength="80"/></label><label>当前密码<input v-model="currentPassword" type="password" autocomplete="current-password" required maxlength="256"/></label><label>新密码（可选）<input v-model="newPassword" type="password" autocomplete="new-password" minlength="12" maxlength="256" placeholder="留空保留当前密码"/></label><label v-if="newPassword">确认新密码<input v-model="confirmPassword" type="password" autocomplete="new-password" required minlength="12" maxlength="256"/></label><div class="form-actions"><span></span><button class="button" :disabled="busy">保存管理员账号</button></div></form></article><article class="panel form-panel"><h2>运行诊断</h2><div v-if="!data.diagnostics.length" class="empty compact-empty"><Check :size="18"/> 暂无异常记录</div><div v-for="(entry,i) in data.diagnostics" :key="i" class="diagnostic"><span>{{ clock(entry.at) }}</span><span>{{ entry.message }}</span></div></article></div><aside class="settings-aside"><article class="aside-note"><Activity :size="25"/><h3>Session Watch</h3><p>独立的 Xray 活动观察与通知面板。</p><p>版本 {{ data.version }}<br/>单节点 · SQLite · Telegram</p><p>在线状态遵循上游核心与面板实现，不代表设备是否联网。连接记录数也不等于网页访问次数。</p></article></aside></div>
        </template>
        <footer class="page-footer"><span><ShieldCheck :size="13"/>只读观察，自主掌控</span><span>Session Watch · 本地工作空间 <span class="footer-divider">/</span> {{ data.settings.timezone }}</span></footer>
      </div>
    </section>
  </div>
  <div v-else-if="authenticated" class="boot"><span>无法加载控制台数据</span><button class="button" @click="action(async()=>{await refresh(true)})">重新加载</button></div>
  <div v-if="toast" class="toast" :class="{error:toastError}" role="status"><Check v-if="!toastError" :size="18"/><CircleHelp v-else :size="18"/>{{ toast }}</div>
  <div v-if="selected || notification || editUser || helpOpen || detailLoading || resumeOpen" class="modal-overlay" @click.self="selected=null;notification=null;editUser=null;helpOpen=false;resumeOpen=false"><section role="dialog" aria-modal="true" aria-label="详细信息" class="modal" :class="{'wide-modal':selected}">
    <button class="modal-close icon-button" @click="selected=null;notification=null;editUser=null;helpOpen=false;resumeOpen=false" aria-label="关闭"><X :size="21"/></button>
    <div v-if="detailLoading" class="empty"><LoaderCircle class="spin"/>正在读取活动详情</div>
    <template v-else-if="selected"><span class="eyebrow">SESSION #{{ selected.id }}</span><h2>活动详情</h2><p class="muted">{{ selected.email }} · {{ clock(selected.started_at,true) }}</p><div class="detail-metrics"><div><span>连接记录</span><strong>{{ selected.requests }}</strong></div><div><span>来源 IP</span><strong>{{ selected.ips.length }}</strong></div><div><span>活动时长</span><strong>{{ duration(selected.started_at,selected.ended_at||selected.offline_since) }}</strong></div></div><div class="detail-columns"><div><h3>主要访问目标</h3><div v-for="target in selected.targets" :key="target.value" class="detail-target"><span>{{ target.value }}</span><strong>×{{ target.count }}</strong></div><p v-if="!selected.targets.length" class="muted">暂无访问记录</p></div><div><h3>汇总预览</h3><pre class="plain-message">{{ selected.offline_message }}</pre></div></div></template>
    <template v-else-if="notification"><span class="eyebrow">NOTIFICATION #{{ notification.id }}</span><h2>{{ notification.kind==='online'?'上线提醒':'下线汇总' }}</h2><p class="muted">{{ notificationLabels[notification.status] }} · {{ clock(notification.created_at,true) }}</p><pre class="plain-message">{{ notification.text }}</pre></template>
    <template v-else-if="editUser"><span class="eyebrow">CLIENT PREFERENCES</span><h2>编辑客户端</h2><p class="muted">{{ editUser.email }}</p><form @submit.prevent="saveUser"><label>显示名称<input v-model="alias" maxlength="80" placeholder="例如：LIN"/></label><label class="switch-label"><span>监控此账号</span><input v-model="monitored" class="switch" type="checkbox"/></label><p class="muted small-text">暂停会结束当前统计且不发送下线提醒；恢复后先建立状态基线。</p><button class="button primary full" :disabled="busy">保存客户端设置</button></form></template>
    <template v-else-if="resumeOpen"><span class="eyebrow">RESUME NOTIFICATIONS</span><h2>恢复推送</h2><p class="muted">当前有 {{ data?.delivery.pending||0 }} 条待发、{{ data?.delivery.failed||0 }} 条失败提醒。选择如何处理已有积压。推送开关与监控开关相互独立。</p><div class="resume-options"><button class="button primary" :disabled="busy" @click="switchDelivery('keep')">保留积压并恢复</button><button class="button" :disabled="busy" @click="switchDelivery('discard')">忽略积压后恢复</button></div><p class="muted small-text">忽略只停止这些记录的后续投递，通知历史仍保留；失败消息保留后需在通知中心重试。</p></template>
    <template v-else-if="helpOpen"><span class="eyebrow">HOW IT WORKS</span><h2>在连接之间，保持知情。</h2><div class="help-steps"><div><span>01</span><div><h3>查询状态</h3><p>本机接入查询 Xray 在线状态，并自动同步 3x-ui 客户端；远程接入查询面板在线列表。首次启动先建立基线，已有在线用户不补发上线消息。</p></div></div><div><span>02</span><div><h3>只读统计</h3><p>读取新增访问日志，记录用户的 IP、入站和目标。日志读取异常时，统计可能不完整。</p></div></div><div><span>03</span><div><h3>确认后推送</h3><p>连续离线满 5 分钟后汇总；期间恢复则继续同一轮。接口异常或程序停机期间不计作连续离线。</p></div></div></div><p class="muted small-text">关闭浏览器不影响后台监控。消息采用持久化重试；网络响应丢失时存在重复投递的可能。</p></template>
  </section></div>
</template>
