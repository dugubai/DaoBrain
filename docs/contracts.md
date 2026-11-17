# 模块接口契约

实际模型以各服务OpenAPI和`common/schemas.py`为准。金额用Decimal，JSON序列化为字符串；来源带zone、version和content_hash。公共健康端点只证明进程响应。普通业务接口鉴权失败401，密钥缺失503；参数不合法422。

## 入口与编排

①`GET /`提供同源门户。`POST /v1/query`接受TaskEnvelope并要求X-API-Key，返回Answer。query必填并限长，task_id可由服务器生成；客户端zone和gate_approved不能作为公开批准。Answer包含verification、refused、model、trace、simulated及有限出处，不返回未通过候选正文。

②`POST /plan`接受TaskEnvelope，要求X-Internal-Key，返回Answer而非Candidate。仅处理服务端认可的公开样本话题，按额度检索、生成、验证；无命中和未通过均返回空答案的拒答。④`POST /dispatch`接受TaskEnvelope，要求内网鉴权，返回本地链路Answer；UNKNOWN失败关闭，RED只执行合成样本路径并被门禁拒绝公开释放。

## 检索与生成

③、⑦、⑧的`POST /retrieve`接受RetrievalRequest(query,top_k)，要求X-Internal-Key，返回Retrieved数组。top_k限制1—20，无命中返回空数组。三者分别绑定公开、本地绿区和合成红区目录，不接受客户端随意指定数据区。

⑤`POST /generate`接受GenReq(task_id,query,zone,approval_proof,evidence)，需要X-Internal-Key以及服务端短期签名。gate_approved保留作旧请求兼容但不授权。返回answer/model/simulated字典，只有stub实现；伪签名、污染证据或未公开内容403，live503。

⑥`POST /infer`接受InferRequest，返回Candidate；可信绿区stub为摘录，Ollama为可选本地适配。⑪`POST /reason`接受相同格式，只处理合成RED证据并返回模拟Candidate，不使用真实微调权重。⑨`POST /gate`接受Candidate；红区或未知来源拒绝，可信绿区候选在严格摘录规则下可返回Candidate。上述接口均要求X-Internal-Key。

⑩`POST /verify`接受Candidate，要求X-Internal-Key，返回VerificationResult。PASS代表当前模拟规则通过，不等同临床或全面事实核验。调用方须同时核对任务ID、出处/合规标志；REVISE补证后仍须重验，不能直接交付答案。

## 应用组件

`corpus_kb`的`POST /search`提供公开可信词法检索，`GET /sources/{source_id}`提供精确可信来源和版本；接口需要内网鉴权，未知来源404。该接口不是已完成Qdrant向量检索。

`card_annotator`使用用户API密钥提交和列举字卡，内网审核身份执行`POST /review`。具体卡片、修订和版本字段见其OpenAPI；卡片状态只在进程内保存，真实用户/审核员身份系统尚未接入。不得将用户自填身份或授权标签视为真实审核证明。

`ocr`的`POST /recognize`要求内网鉴权，显式文本模拟输入生成待复核结果；图片识别返回501。不得传入文件路径或任意URL，让服务绕过目录权限。`portal`使用同源`/v1/query`及只在内存中保存的密钥，不使用浏览器持久化存储。

## 验收限制

此契约描述已编写和本机模拟测试的接口，不声称数据库持久化、真实OCR、微调、外部模型、GPU、小程序或生产权限已经完成。开发机部署和Git上传仍在磁盘与认证阻塞解除后验收。
