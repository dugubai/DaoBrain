# DaoBrain 数据大盘公网访问（主站跨机反代）

## 拓扑
备机(A10 GPU, 8.147.57.169) 运行完整数据面 + ⑦ dashboard，仅监听备机回环 127.0.0.1:8020。
备机安全组只放行 22，故大盘不直接对外。改由主机(39.102.49.1, 已放行 80) 反代对外。

链路：浏览器 → 主机 nginx:80 `location ^~ /daobrain/dash/`
      → 主机回环 127.0.0.1:18020
      → 反向 SSH 隧道(备机→主机 sshd:22)
      → 备机 127.0.0.1:8020 dashboard

## 隧道（持久、开机自启）
- 方向：备机主动 ssh -R 推到主机回环（主机→备机:22 被备机安全组拒绝，故只能此方向）。
- 单元文件：ops/daobrain-dash-tunnel.service → /etc/systemd/system/，`systemctl enable --now`。
- 密钥：备机 /root/.ssh/daobrain_tunnel(ed25519)。
- 主机授权：admin 的 authorized_keys 追加一条受限公钥：
  `restrict,port-forwarding,permitopen="127.0.0.1:18020",no-pty,no-X11-forwarding,no-agent-forwarding`
  该密钥仅能建立到主机 127.0.0.1:18020 的端口转发，不能开 shell、不能转发其它端口。原有登录密钥保持不变。
- 主机侧仅监听回环（sshd gatewayports no），18020 不对外。

## nginx（主机）
- ops/nginx/primary.daobrain-dash.location.conf → /data/daobrain/dash/nginx.dash.conf，
  由 /etc/nginx/conf.d/zjg.conf 第 14 行 `include` 引入（与 /manage/ 同级）。
- ops/nginx/primary.daobrain-portal.location.conf → /root/DaoBrain/public_demo/nginx.locations.active.conf
  在 `/daobrain/` 门户 location 内用 sub_filter 注入主页导航（数据流量大盘 / 管理控制台），
  同步注入模板 nginx.locations.conf 以便重新部署后仍保留。
- dashboard 前端用相对路径 fetch('api/overview')，故 /daobrain/dash/ 子路径无需 URL 重写。

## 验证
curl http://39.102.49.1/daobrain/dash/  → 200
curl http://39.102.49.1/daobrain/dash/api/overview → 200 真实指标
主页 http://39.102.49.1/daobrain/ 含 “数据流量大盘 →” 与 “管理控制台 →” 链接

## 回滚
主机：删除 zjg.conf 中 dash include 行 + nginx -t && systemctl reload nginx（或还原 .bak.dash.* ）。
      还原 active.conf.bak.nav.* 去掉主页 nav。
备机：systemctl disable --now daobrain-dash-tunnel.service。
主机：从 admin authorized_keys 删除带 daobrain-dash-tunnel@backup 的那一行。
