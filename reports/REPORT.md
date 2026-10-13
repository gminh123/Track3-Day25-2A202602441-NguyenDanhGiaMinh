# Lab 25B — Game Day Report

**Họ tên / nhóm:** Nguyễn Danh Gia Minh / 2A202602441  
**Commit:** Chưa commit; HEAD tham chiếu: `a3f9ce7078c14d183ed1e95d311a2df59bc86855`  
**Điểm tự động (`make grade`):** 84 / 88

> Kết quả dưới đây là lần chạy local với seed 25, ngoại trừ các thí nghiệm mục 3 và 8 được ghi riêng. Băng ẩn chưa được cung cấp trong môi trường này.

## 1. Kết quả Game Day (naive vs của bạn)

| Băng | Client | availability | p95 (s) | amplification | retry_ratio | cost/turn | recovery (s) | PASS? |
|---|---|---:|---:|---:|---:|---:|---:|:---:|
| G1 | naive | 0.7194 | 7.157 | 7.738 | 6.738 | 2.163 | 4 | Không |
| G1 | yours | 1.0000 | 4.780 | 1.063 | 0.001 | 2.104 | 0 | Có |
| G2 | naive | 0.4560 | 15.034 | 9.904 | 8.904 | 4.627 | 155 | Không |
| G2 | yours | 1.0000 | 9.847 | 1.037 | 0.005 | 2.105 | 0 | Có |
| G3 | naive | 1.0000 | 4.768 | 1.164 | 0.164 | 2.328 | 0 | Không |
| G3 | yours | 1.0000 | 4.718 | 1.065 | 0.035 | 2.146 | 0 | Có |
| G4 | naive | 1.0000 | 13.764 | 1.004 | 0.004 | 2.008 | 0 | Không |
| G4 | yours | 1.0000 | 5.684 | 1.013 | 0.002 | 2.069 | 0 | Có |
| G5 | naive | 0.3699 | 5.359 | 17.030 | 16.030 | 2.124 | — | Không |
| G5 | yours | 0.8179 | 5.477 | 1.003 | 0.002 | 2.504 | — | Có |
| G6 | naive | 0.5170 | 4.836 | 12.344 | 11.344 | 2.009 | 0 | Không |
| G6 | yours | 1.0000 | 6.099 | 1.053 | 0.017 | 2.202 | 0 | Có |

`recovery_s=0` nghĩa là metric không phát hiện giai đoạn hồi phục kéo dài sau thời điểm restore; `—` là băng không định nghĩa `restore_at`. Naive chỉ PASS 0/6 băng; resilient PASS 6/6 với seed 25, và cũng PASS 6/6 ở seed 1 và 42. Với G2, resilient giảm amplification từ 9.904 xuống 1.037 và cost/good turn từ 4.627 xuống 2.105.

## 2. Cấu hình client và lý do

| Tham số | Giá trị | Vì sao (dựa trên băng nào, số liệu nào) |
|---|---:|---|
| `attempt_timeout_s` | 8 s | G6 có provider treo; 8 s giới hạn thời gian chờ để còn thời gian failover trước deadline 30 s. G6 đạt availability 1.0, p95 6.099 s. |
| `max_attempts_per_provider` | 2 | Cho phép tối đa một retry cùng provider trước failover; G2 amplification 1.037 và G3 retry ratio 0.035, thấp hơn xa naive 9.904 và 0.164. |
| `backoff_base_s / cap_s` | 0.25 / 4 s | Jitter tránh các client retry đồng bộ; exponential cap ngăn delay phình quá lớn. G1 có 0 vi phạm `retry-after` và amplification 1.063. |
| `retry_budget_ratio` | 0.1 | Giới hạn retry theo lưu lượng gốc. Thí nghiệm budget-off làm G2 amplification tăng 1.382 → 1.591, retry ratio 0.058 → 0.382. |
| `breaker.window_size / min_calls` | 20 / 10 | Cửa sổ đủ lớn để dùng tỷ lệ thay vì phản ứng theo vài lỗi lẻ; G2 recovery của client là 0 s sau restore. |
| `breaker.failure_rate_threshold` | 0.5 | Mở breaker khi ít nhất một nửa kết quả trong cửa sổ lỗi; tránh tiếp tục dồn request vào deployment đang lỗi. |
| `breaker.slow_call_s` | 5 s | G4 thêm 5 s latency nhưng không phát lỗi; breaker xem call chậm là tín hiệu sức khỏe xấu. Client giữ p95 5.684 s, naive là 13.764 s. |
| `breaker.open_duration_s` | 10 s | Tạm ngừng provider lỗi rồi thử lại thay vì liên tục gửi request; G2 phục hồi ngay theo metric sau restore. |
| `breaker.half_open_max_probes` | 2 (successes=2) | Giới hạn probe đồng thời và chỉ đóng breaker sau hai probe thành công; tránh tạo burst vào provider vừa phục hồi. |

Sơ đồ quyết định của `on_error`:

| Loại lỗi | Ghi breaker? | Retry cùng provider? | Failover? | Cooldown |
|---|---|---|---|---|
| Client error (4xx) | Release permit, không ghi lỗi | Không | Không | Không |
| Rate limit (429) | Release permit | Không | Có, ngay | Theo `retry-after` |
| Quota exhausted (429) | Release permit | Không | Có, ngay | `quota_cooldown_s` |
| Overloaded (503/529) | Ghi failure | Có nếu còn retry budget, chưa mở breaker và đủ deadline | Nếu không retry được | Không |
| Server error (các 5xx khác) | Ghi failure | Có nếu đủ điều kiện | Nếu không retry được | Không |
| Timeout | Ghi failure | Có nếu đủ điều kiện | Nếu không retry được | Không |

## 3. Ba thí nghiệm

1. **Retry budget là lớp phòng thủ cuối.** Đặt `breaker.min_calls=1,000,000,000` để breaker không trip và `max_attempts_per_provider=5`. Giữ budget: G2 amplification 1.382, retry ratio 0.058, recovery 0 s; G3 lần lượt là 1.135, 0.067, 0 s. Tắt budget bằng các giá trị lớn theo đề bài: G2 tăng thành 1.591 / 0.382 / 22 s; G3 retry ratio tăng thành 0.160 và fail check ≤0.10. Khi breaker hoạt động bình thường, breaker bảo vệ từng provider còn budget giới hạn tổng retry khi breaker cấu hình sai, chưa kịp mở hoặc nhiều lỗi cùng lúc; hai lớp kiểm soát khác nhau nên cần giữ cả hai.

2. **`attempt_timeout_s: 30`.** Trên G6 availability giảm còn 0.9488 (2.263/2.385 lượt tốt), có 108 give-up vì `deadline` và 14 vì `no_provider_available`; p95 của các lượt thành công là 5.298 s. p95 thấp không có nghĩa trải nghiệm tốt hơn: các request chờ lâu rồi thất bại bị loại khỏi mẫu p95. Đối chứng G4 vẫn availability 1.0, p95 5.683 s vì request chậm thêm nhưng hoàn tất, không chạm timeout. Timeout nên dựa vào phân phối latency bình thường (đặc biệt p99), deadline user và ngân sách còn lại để failover; timeout 8 s giữ thời gian cho provider kế tiếp trong deadline 30 s.

3. **Bỏ lọc tool calling khỏi `ladder()`.** Trong G6 có 244 `incompatible_routes`; availability giảm còn 0.8977 (2.141/2.385 lượt tốt), trong đó có 451 attempt gửi đến `gamma-mini`. Với góc nhìn sản phẩm: 244 yêu cầu cần thao tác bằng tool đã được định tuyến tới model không có khả năng đó, nên khách hàng không nhận được kết quả dù model vẫn trả lời văn bản.

## 4. Workflow hoàn tiền

| Executor | completion | crashes | duplicate refunds | duplicate emails | lost refunds | overpaid | wasted LLM / crash |
|---|---:|---:|---:|---:|---:|---:|---:|
| naive | 1.000 | 81 | 128 | 81 | 0 | 1,496,700 cents | 1.123 |
| yours | 1.000 | 66 | 0 | 0 | 0 | 0 cents | 0.303 |

Idempotency key một mình ngăn thực thi side effect trùng, nhưng không giúp worker biết các bước nào đã hoàn tất; sau crash, workflow vẫn có thể chạy lại và lãng phí LLM/read. Checkpoint một mình không giải quyết cửa sổ “payment đã commit nhưng worker chết trước checkpoint”: khi resume, payment sẽ bị gửi lại và hoàn tiền trùng. Vì vậy checkpoint phải được ghi **sau** thành công, còn side effect phải dùng key ổn định; nếu ghi checkpoint trước payment, crash giữa hai thao tác khiến workflow tin rằng đã hoàn tiền dù payment chưa chạy, gây hoàn tiền bị mất.

## 5. Alerting & error budget

| Sự cố | Mong đợi | Naive alerter | Burn-rate alerter | Độ trễ phát hiện |
|---|---|---|---|---:|
| checkout_bug | page | Page, +1 phút | Page, +27 phút | 27 phút |
| provider_outage | page | Page, +0 phút | Page, +4 phút | 4 phút |
| deploy_blip | none | False page | Không alert | — |
| slow_leak | ticket | Không phát hiện | Ticket, +1,337 phút | 1,337 phút |
| degraded_evening | page | Page, +214 phút | Page, +291 phút | 291 phút |
| night_spike | none | False page | Không alert | — |
| small_bump | none | False page | Không alert | — |

Naive tạo 7 page, 0 ticket, 3 false page; burn-rate tạo 3 page, 1 ticket, 0 false page. Trong 30 ngày có 25,901,755 request, 55,618 request lỗi; error budget cho phép 129,508.8 lỗi. Đã tiêu 42.95%, còn 57.05%; không cần freeze release (`freeze_releases=false`). `small_bump` tương đương khoảng 2.4 phút outage toàn phần trên 30 ngày, xấp xỉ 1.1% error budget, và ngắn hơn điều kiện hai cửa sổ page; không nên đánh thức on-call vì sự cố tự hết mà mức tiêu budget nhỏ.

## 6. Postmortem — Băng G2 với client naive (blameless)

![G2: attempts/s và rolling success ratio](./gameday/G2_overload_storm.png)

**Tóm tắt (3 câu):** Từ giây 120, alpha mất 75% năng lực xử lý và hàng đợi tăng, trong khi client naive tiếp tục retry cùng provider. Trong 2,362 lượt user, chỉ 1,077 lượt thành công (availability 45.6%); amplification đạt 9.904 attempt/call và cost mỗi lượt tốt là 4.627. Alpha được khôi phục ở giây 300, nhưng tải tồn đọng cùng các lần retry tiếp tục giữ hệ thống trong trạng thái suy giảm; metric chỉ ghi nhận recovery sau 155 giây.

**Ảnh hưởng:** 1,285 lượt user thất bại. Các lượt đầu tiên bị ảnh hưởng bắt đầu khoảng t=120.6; lượt lỗi đầu hoàn tất ở t=142.8 và lượt lỗi cuối ở t=475.2, tức khoảng 355 giây từ lượt bị ảnh hưởng đầu đến lỗi cuối. Giả sử tháng có 4 lượt/giây trong 30 ngày, error budget ở SLO 99.5% là 51,840 lượt lỗi; sự cố tiêu khoảng 2.48% budget tháng đó.

**Timeline** (giây ảo — từ `naive_G2_overload_storm_timeseries.csv`; rolling ratio là 10 giây):

| t | Sự kiện | Bằng chứng (số) |
|---:|---|---|
| 120 | Alpha mất 75% năng lực | Bắt đầu incident; 5 attempt tới alpha trong giây 120. |
| 130 | Queue bắt đầu làm mất khả năng hoàn tất | 10 attempt alpha/giây; rolling success 10 giây là 28/28 trước khi backlog biểu hiện rõ. |
| 150 | Retry storm đã hình thành | 163 attempt alpha/giây; rolling success 0/50. |
| 180 | Người dùng vẫn nhận kết quả lỗi | 138 attempt alpha/giây; rolling success 0/32. |
| 299 | Ngay trước khi provider hồi phục | 136 attempt alpha/giây; 0/41 call hoàn tất gần đó thành công. |
| 300 | Alpha hồi phục hoàn toàn | Vẫn có 115 attempt alpha/giây và 0/43 rolling success; tải client còn áp đảo năng lực vừa phục hồi. |
| 390 | Backlog/retry tiếp tục tái tạo tải | 139 attempt alpha/giây; rolling success 0/42. |
| 420 | Tải giảm nhưng chưa hồi phục | 53 attempt alpha/giây; rolling success vẫn 0/36. |
| 455 | Bắt đầu cửa sổ recovery đạt tiêu chí | 13 attempt alpha/giây; 4/5 call kết thúc trong giây đó thành công; metric recovery là 155 s sau restore. |
| 470 | Phần lớn lưu lượng đã thành công | 9 attempt alpha/giây; 13/13 call thành công trong giây đó; rolling 10 giây 119/124 = 96.0%. |
| 480 | Trở lại hoạt động bình thường | 8 attempt alpha/giây; 11/11 call thành công, rolling 10 giây 137/137. |

**Trigger vs root cause:** Trigger là alpha mất 75% GPU trong ba phút. Root cause là thiết kế client không giới hạn retry: cùng một logical call có thể khuếch đại thành gần 10 attempt, không có jitter, retry budget, breaker hay failover hữu ích. Khi client timeout, server vẫn xử lý request cũ; retry thêm request mới khiến hàng đợi lớn hơn, tạo vòng lặp tự duy trì. Sau t=300, request cũ trong queue vẫn chiếm slot và các retry tiếp tục được gửi; provider đã hồi phục nhưng tải do client tạo ra chỉ giảm dần, nên recovery kéo dài đến t=455.

**Điều gì giúp / điều gì làm tệ hơn:** Provider tự phục hồi năng lực tại t=300 và lưu lượng retry cuối cùng cũng giảm, nhưng không có cơ chế phía client để ngắt sớm vòng lặp. Retry lặp lại ngay cùng provider làm lãng phí công xử lý; thời gian xử lý request cũ không bị hủy sau timeout nên backlog vẫn tiếp tục tiêu thụ capacity. Resilient client đối chứng có availability 100%, amplification 1.037 và recovery 0 s.

**Action items**:

| # | Hành động | Loại (prevent / detect / mitigate) | Owner | Cách kiểm chứng |
|---|---|---|---|---|
| 1 | Duy trì một tầng retry với full jitter, retry budget 10% và giới hạn tối đa 2 attempt/provider. | Prevent | Client Platform | CI chạy G2/G3 nhiều seed; amplification ≤1.6 và retry ratio G3 ≤0.10. |
| 2 | Bật sliding-window breaker có slow-call detection, HALF-OPEN probe cap và cooldown theo provider. | Mitigate | Reliability Engineering | G2 recovery ≤30 s, G4 p95 ≤10 s và alpha share sau restore ≥80%. |
| 3 | Dashboard/alert theo success ratio, queue latency, attempt amplification và retry ratio; diễn tập runbook. | Detect | SRE On-call | Alert burn-rate theo hai cửa sổ; game day không có false page và G2 được phát hiện trong 5 phút. |

## 7. Một điểm yếu còn lại

G2 p95 của client là 9.847 s, chỉ còn 0.153 s biên dưới ngưỡng 10 s; đây là biên nhỏ trước khi tăng tải hoặc biến thiên seed. Bước tiếp theo là thêm admission control/giới hạn concurrency khi spillover, đo p95 và queue wait theo provider, rồi chạy lại G2/G6 trên nhiều seed với mục tiêu p95 G2 <9 s mà availability vẫn ≥95%.

## 8. Cache (Part G)

| Client | availability | faq_availability | cache_hit_ratio | stale_served | wrong_answers | outdated_answers | cost/turn | PASS? |
|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| resilient (không cache) | 0.8373 | 0.8424 | 0.000 | 0 | 0 | 0 | 1.386 | Không |
| cached (của bạn) | 0.9124 | 0.9645 | 0.652 | 157 | 0 | 3 | 0.915 | Có |

| Tham số `cache:` | Giá trị | Vì sao (số liệu nào) |
|---|---:|---|
| `similarity_threshold` | 0.90 | Với TTL 60 s và stale window 300 s, ngưỡng 0.80 tạo 157 câu trả lời sai do `other_question`; 0.90 và 0.97 đều không có câu sai. Chọn 0.90 để giữ hit ratio 0.652 thay vì 0.581 ở 0.97. |
| `ttl_s` | 60 s | Ở ngưỡng 0.90, TTL 30/60/300 s lần lượt cho hit ratio 0.592/0.652/0.731; outdated answers là 2/3/95. TTL 60 giữ outdated ≤15 mà vẫn đạt FAQ availability 0.9645. |
| `max_stale_s` | 300 s | Ở threshold 0.90/TTL 60, stale window 0 làm FAQ availability 0.8733 và không phục vụ stale; 300 s nâng availability lên 0.9645, phục vụ 157 câu stale, vẫn chỉ có 3 outdated answers. |

**Thí nghiệm:**

1. **Ngưỡng.** Với TTL 60 s, stale 300 s: threshold 0.80 cho hit ratio 0.666, 181 câu sai (`other_question`), cost/turn 0.965; threshold 0.90 cho 0 câu sai, hit 0.652, cost 0.915; threshold 0.97 cho 0 câu sai, hit 0.581, cost 0.964. Ngưỡng thấp ghép các FAQ gần nghĩa nhưng khác ý định (ví dụ chính sách hoàn tiền và thời hạn hoàn tiền); 0.90 là lựa chọn cân bằng giữa an toàn và hit ratio.
2. **TTL.** Ở threshold 0.90 và stale 300 s, TTL 30/60/300 cho `outdated_answers` 2/3/95 và hit ratio 0.592/0.652/0.731. Webhook chỉ giúp với tài liệu có thông báo; thay đổi bảng phí ship ở C1 không báo trước nên cache không nhận invalidate, TTL là giới hạn tuổi của câu trả lời đó.
3. **Stale-if-error.** Với threshold 0.90 và TTL 60, `max_stale_s=0` làm FAQ availability còn 0.8733, cache hit ratio 0.561 và stale served 0. Tăng cửa sổ lên 300 s nâng FAQ availability lên 0.9645, hit ratio 0.652 và phục vụ 157 câu trả lời stale; `wrong_answers=0`.

- Câu hỏi cá nhân không được cache vì câu trả lời phụ thuộc dữ liệu tài khoản có thể thay đổi, chứa thông tin nhạy cảm và có nguy cơ rò rỉ do scope/key/embedding sai; cache theo user vẫn có thể trả dữ liệu cũ hoặc bị lộ khi định danh/scope bị xử lý sai.
- Khi cache giữ dashboard availability xanh trong outage, cần alert riêng trên provider error rate/availability, retry/failover rate và tỷ lệ stale response; theo dõi tuổi cache và synthetic uncached probe để trạng thái cache không che khuất tình trạng provider.
