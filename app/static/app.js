/* csgoMarket 前端：无框架、无构建步骤、无 CDN。
 * 安全约定：所有来自接口的文本一律通过 textContent / setAttribute 写入，
 * 不使用 innerHTML 拼接未转义数据。
 */
(function () {
    "use strict";

    var TIMEOUT_MS = 8000;
    var MAX_ITEMS = Number(document.body.dataset.maxItems) || 200;

    var els = {
        addForm: document.getElementById("addForm"),
        goodsInput: document.getElementById("goodsInput"),
        addBtn: document.getElementById("addBtn"),
        refreshBtn: document.getElementById("refreshBtn"),
        refreshLabel: document.getElementById("refreshLabel"),
        refreshedAt: document.getElementById("refreshedAt"),
        errorBanner: document.getElementById("errorBanner"),
        errorText: document.getElementById("errorText"),
        retryBtn: document.getElementById("retryBtn"),
        failedBanner: document.getElementById("failedBanner"),
        failedText: document.getElementById("failedText"),
        statsBar: document.getElementById("statsBar"),
        statCount: document.getElementById("statCount"),
        statOk: document.getElementById("statOk"),
        statFailedChip: document.getElementById("statFailedChip"),
        statFailed: document.getElementById("statFailed"),
        statTook: document.getElementById("statTook"),
        tableWrap: document.getElementById("tableWrap"),
        tableBody: document.getElementById("tableBody"),
        loadingState: document.getElementById("loadingState"),
        emptyState: document.getElementById("emptyState"),
        emptyTitle: document.getElementById("emptyTitle"),
        emptyText: document.getElementById("emptyText")
    };

    var state = {
        items: [],
        failed: [],
        count: 0,
        loading: false,
        adding: false
    };

    /* ---------------- 通用工具 ---------------- */

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) {
            node.className = className;
        }
        if (text !== undefined && text !== null) {
            node.textContent = String(text);
        }
        return node;
    }

    function isNumber(value) {
        return typeof value === "number" && isFinite(value);
    }

    function formatPrice(value) {
        return isNumber(value) ? "¥" + value.toFixed(2) : "—";
    }

    function formatCount(value) {
        if (!isNumber(value)) {
            return "—";
        }
        return value.toLocaleString("zh-CN");
    }

    function pad(value) {
        return value < 10 ? "0" + value : String(value);
    }

    function formatDateTime(iso) {
        if (!iso) {
            return "—";
        }
        var date = new Date(iso);
        if (isNaN(date.getTime())) {
            return String(iso);
        }
        return (
            date.getFullYear() +
            "-" +
            pad(date.getMonth() + 1) +
            "-" +
            pad(date.getDate()) +
            " " +
            pad(date.getHours()) +
            ":" +
            pad(date.getMinutes())
        );
    }

    function ApiError(message, status) {
        this.name = "ApiError";
        this.message = message;
        this.status = status;
    }
    ApiError.prototype = Object.create(Error.prototype);
    ApiError.prototype.constructor = ApiError;

    /* ---------------- 网络请求 ---------------- */

    function apiFetch(url, options) {
        var controller = new AbortController();
        var timer = setTimeout(function () {
            controller.abort();
        }, TIMEOUT_MS);

        var config = Object.assign({}, options || {}, {
            signal: controller.signal,
            headers: Object.assign(
                { Accept: "application/json" },
                (options && options.headers) || {}
            )
        });

        return fetch(url, config)
            .then(function (response) {
                return response.text().then(function (text) {
                    var data = null;
                    if (text) {
                        try {
                            data = JSON.parse(text);
                        } catch (err) {
                            if (response.ok) {
                                throw new ApiError("服务返回了无法解析的数据", response.status);
                            }
                        }
                    }
                    if (!response.ok) {
                        var detail =
                            data && typeof data.detail === "string"
                                ? data.detail
                                : "请求失败（HTTP " + response.status + "）";
                        throw new ApiError(detail, response.status);
                    }
                    return data;
                });
            })
            .catch(function (err) {
                if (err instanceof ApiError) {
                    throw err;
                }
                if (err && err.name === "AbortError") {
                    throw new ApiError("请求超时（8 秒），请检查网络后重试", 0);
                }
                throw new ApiError("网络异常，请确认服务正在运行", 0);
            })
            .then(
                function (data) {
                    clearTimeout(timer);
                    return data;
                },
                function (err) {
                    clearTimeout(timer);
                    throw err;
                }
            );
    }

    /* ---------------- 提示条 ---------------- */

    function showError(message, retryable) {
        els.errorText.textContent = message;
        els.errorBanner.hidden = false;
        els.retryBtn.hidden = !retryable;
    }

    function hideError() {
        els.errorBanner.hidden = true;
        els.errorText.textContent = "";
    }

    function showFailed(items) {
        if (!items.length) {
            els.failedBanner.hidden = true;
            els.failedText.textContent = "";
            return;
        }
        var parts = items.map(function (item) {
            return item.goods_id + "（" + item.reason + "）";
        });
        els.failedText.textContent =
            "以下 " + items.length + " 个商品拉取失败：" + parts.join("、") + "。可稍后点击刷新重试。";
        els.failedBanner.hidden = false;
    }

    /* ---------------- 渲染 ---------------- */

    function skeletonRow() {
        var tr = el("tr", "skeleton-row");
        tr.appendChild(el("td", "cell-icon")).appendChild(el("span", "skeleton icon"));
        tr.appendChild(el("td")).appendChild(el("span", "skeleton w80"));
        tr.appendChild(el("td", "num")).appendChild(el("span", "skeleton w40"));
        tr.appendChild(el("td", "num")).appendChild(el("span", "skeleton w40"));
        tr.appendChild(el("td", "num")).appendChild(el("span", "skeleton w40"));
        tr.appendChild(el("td", "num")).appendChild(el("span", "skeleton w40"));
        tr.appendChild(el("td")).appendChild(el("span", "skeleton w60"));
        tr.appendChild(el("td")).appendChild(el("span", "skeleton w60"));
        tr.appendChild(el("td", "cell-action")).appendChild(el("span", "skeleton w40"));
        return tr;
    }

    function renderSkeleton() {
        els.tableBody.replaceChildren();
        for (var i = 0; i < 4; i += 1) {
            els.tableBody.appendChild(skeletonRow());
        }
        els.tableWrap.hidden = false;
        els.loadingState.hidden = true;
        els.emptyState.hidden = true;
    }

    function iconCell(item) {
        var td = el("td", "cell-icon");
        if (item.icon_url) {
            var img = document.createElement("img");
            img.className = "goods-icon";
            img.loading = "lazy";
            img.alt = "";
            img.referrerPolicy = "no-referrer";
            img.src = item.icon_url;
            img.addEventListener("error", function () {
                var fallback = el(
                    "span",
                    "goods-icon-fallback",
                    (item.name || "?").slice(0, 1)
                );
                if (img.parentNode) {
                    img.parentNode.replaceChild(fallback, img);
                }
            });
            td.appendChild(img);
        } else {
            td.appendChild(
                el("span", "goods-icon-fallback", (item.name || "?").slice(0, 1))
            );
        }
        return td;
    }

    function nameCell(item) {
        var td = el("td");
        var wrap = el("div", "goods-name");
        var link = el("a", null, item.name || "未知饰品");
        link.href = "https://buff.163.com/goods/" + encodeURIComponent(item.goods_id);
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        wrap.appendChild(link);
        wrap.appendChild(el("span", "goods-id", "ID " + item.goods_id));
        td.appendChild(wrap);
        return td;
    }

    function priceCell(value, extraClass) {
        var td = el("td", "num");
        td.appendChild(el("span", "price " + (extraClass || ""), formatPrice(value)));
        return td;
    }

    function spreadCell(buffPrice, steamPrice) {
        var td = el("td", "num");
        if (!isNumber(buffPrice) || !isNumber(steamPrice) || steamPrice <= 0) {
            td.appendChild(el("span", "price-muted", "—"));
            return td;
        }
        var pct = ((buffPrice - steamPrice) / steamPrice) * 100;
        var className = "spread";
        if (pct < 0) {
            className += " spread-down";
        } else if (pct > 0) {
            className += " spread-up";
        }
        var badge = el("span", className, (pct > 0 ? "+" : "") + pct.toFixed(1) + "%");
        badge.title =
            pct <= 0
                ? "BUFF 比 Steam 低 " + Math.abs(pct).toFixed(1) + "%"
                : "BUFF 比 Steam 高 " + pct.toFixed(1) + "%";
        td.appendChild(badge);
        return td;
    }

    function topPricesCell(prices) {
        var td = el("td");
        var list = Array.isArray(prices) ? prices : [];
        if (!list.length) {
            td.appendChild(el("span", "empty-prices", "暂无卖单"));
            return td;
        }
        var wrap = el("div", "top-prices");
        list.slice(0, 5).forEach(function (price) {
            wrap.appendChild(el("span", "price-pill", formatPrice(price)));
        });
        td.appendChild(wrap);
        return td;
    }

    function actionCell(item) {
        var td = el("td", "cell-action");
        var button = el("button", "btn btn-danger", "删除");
        button.type = "button";
        button.setAttribute("aria-label", "取消关注 " + (item.name || item.goods_id));
        button.addEventListener("click", function () {
            removeGoods(item, button);
        });
        td.appendChild(button);
        return td;
    }

    function renderRow(item) {
        var tr = document.createElement("tr");
        tr.appendChild(iconCell(item));
        tr.appendChild(nameCell(item));
        tr.appendChild(priceCell(item.buff_min_price, "price-buff"));
        tr.appendChild(priceCell(item.steam_price_cny, "price-muted"));
        tr.appendChild(spreadCell(item.buff_min_price, item.steam_price_cny));
        var sellTd = el("td", "num");
        sellTd.appendChild(el("span", "sell-count", formatCount(item.sell_count)));
        tr.appendChild(sellTd);
        tr.appendChild(topPricesCell(item.top_prices));
        var timeTd = el("td");
        timeTd.appendChild(el("span", "time-cell", formatDateTime(item.fetched_at)));
        tr.appendChild(timeTd);
        tr.appendChild(actionCell(item));
        return tr;
    }

    function renderEmpty(title, text) {
        els.tableBody.replaceChildren();
        els.tableWrap.hidden = true;
        els.loadingState.hidden = true;
        els.emptyTitle.textContent = title;
        els.emptyText.textContent = text;
        els.emptyState.hidden = false;
    }

    function render() {
        var items = state.items;
        var failed = state.failed;

        showFailed(failed);
        els.statsBar.hidden = state.count === 0;
        els.statCount.textContent = String(state.count);
        els.statOk.textContent = String(items.length);
        els.statFailed.textContent = String(failed.length);
        els.statFailedChip.hidden = failed.length === 0;

        els.tableBody.replaceChildren();
        items.forEach(function (item) {
            els.tableBody.appendChild(renderRow(item));
        });

        if (items.length > 0) {
            els.tableWrap.hidden = false;
            els.emptyState.hidden = true;
            els.loadingState.hidden = true;
            return;
        }

        if (state.count > 0 && failed.length > 0) {
            renderEmpty("暂时没有可显示的数据", "关注列表中的商品都拉取失败了，请稍后点击刷新重试。");
        } else {
            renderEmpty(
                "还没有关注任何饰品",
                "在上方输入 BUFF 商品 ID，添加第一个监控对象（最多 " + MAX_ITEMS + " 个）。"
            );
        }
    }

    function setLoading(loading) {
        state.loading = loading;
        els.refreshBtn.disabled = loading;
        els.refreshBtn.classList.toggle("is-busy", loading);
        els.refreshLabel.textContent = loading ? "刷新中" : "刷新";

        if (loading && state.items.length === 0) {
            renderSkeleton();
        } else if (!loading && state.items.length === 0 && !els.emptyState.hidden) {
            els.loadingState.hidden = true;
        }
    }

    /* ---------------- 业务动作 ---------------- */

    function loadWatchlist(force) {
        if (state.loading) {
            return Promise.resolve();
        }
        hideError();
        setLoading(true);
        var url = "/api/v1/watchlist" + (force ? "?refresh=true" : "");

        return apiFetch(url)
            .then(function (data) {
                state.items = Array.isArray(data.items) ? data.items : [];
                state.failed = Array.isArray(data.failed) ? data.failed : [];
                state.count = isNumber(data.count) ? data.count : state.items.length;
                els.refreshedAt.textContent =
                    "更新于 " +
                    formatDateTime(data.fetched_at) +
                    " · " +
                    Math.round(data.took_ms || 0) +
                    " ms";
                els.statTook.textContent = String(Math.round(data.took_ms || 0));
                render();
            })
            .catch(function (err) {
                showError(err.message, true);
                if (state.items.length === 0) {
                    renderEmpty("暂时无法获取行情", "请检查网络或服务状态后重试。");
                }
            })
            .then(function () {
                setLoading(false);
            });
    }

    function setAdding(adding) {
        state.adding = adding;
        els.addBtn.disabled = adding;
        els.addBtn.textContent = adding ? "添加中…" : "添加关注";
        els.goodsInput.disabled = adding;
    }

    function addGoods(event) {
        event.preventDefault();
        if (state.adding) {
            return;
        }
        var raw = els.goodsInput.value;
        if (!/^\d{1,12}$/.test(raw)) {
            showError("商品 ID 必须是 1–12 位纯数字。", false);
            els.goodsInput.focus();
            return;
        }

        hideError();
        setAdding(true);
        apiFetch("/api/v1/watchlist", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ goods_id: raw })
        })
            .then(function () {
                els.goodsInput.value = "";
                return loadWatchlist(false);
            })
            .catch(function (err) {
                showError(err.message, false);
            })
            .then(function () {
                setAdding(false);
            });
    }

    function removeGoods(item, button) {
        var label = item.name || "商品 " + item.goods_id;
        if (!window.confirm("确定不再关注「" + label + "」吗？")) {
            return;
        }
        button.disabled = true;
        hideError();
        apiFetch("/api/v1/watchlist/" + encodeURIComponent(item.goods_id), {
            method: "DELETE"
        })
            .then(function () {
                return loadWatchlist(false);
            })
            .catch(function (err) {
                showError(err.message, false);
                button.disabled = false;
            });
    }

    /* ---------------- 绑定 ---------------- */

    els.addForm.addEventListener("submit", addGoods);
    els.refreshBtn.addEventListener("click", function () {
        loadWatchlist(true);
    });
    els.retryBtn.addEventListener("click", function () {
        loadWatchlist(true);
    });
    els.goodsInput.addEventListener("input", function () {
        els.goodsInput.value = els.goodsInput.value.replace(/\D+/g, "").slice(0, 12);
    });

    loadWatchlist(false);
})();
