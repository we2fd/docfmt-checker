/* 格致 · 前端主程序（Vue 3 全局构建 + Element Plus，全部本地资源，零外网） */
(function () {
  'use strict';

  var API = '/api/v1';
  var IGNORE_KEY = 'docfmt.ignored';

  function readIgnored() {
    try {
      var raw = window.localStorage.getItem(IGNORE_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (error) {
      return {};
    }
  }

  function writeIgnored(map) {
    try {
      window.localStorage.setItem(IGNORE_KEY, JSON.stringify(map));
    } catch (error) {
      /* 隐私模式下 localStorage 不可用，忽略即可 */
    }
  }

  var SEVERITY_RANK = { error: 0, warning: 1, info: 2, unknown: 3 };

  var app = Vue.createApp({
    data: function () {
      return {
        view: 'check',
        config: { fix_actions: [], categories: [], checkers: [], scopes: [] },
        health: {},
        stats: null,
        documents: [],
        samples: [],

        current: null,
        report: null,
        paragraphs: [],

        checking: false,
        progress: 0,
        feed: [],
        expectedRuleCount: 56,

        tab: 'issues',
        sevFilter: 'all',
        catFilter: 'all',
        ruleResultFilter: 'all',
        ignored: readIgnored(),
        currentIssueIndex: null,
        targetIndex: null,

        fixing: false,
        fixResult: null,
        fixActions: ['blank_lines', 'punctuation', 'font_size', 'caption_numbering'],

        ruleset: null,
        ruleTab: 'table',
        yamlText: '',
        yamlMessage: '',
        savingYaml: false,
        ruleKeyword: '',
        ruleCat: 'all',

        /* ---- 阈值快速调整：值与原始值分开存，才能算出「改了哪几项」 ---- */
        quickGroups: [],
        quickValues: {},
        quickOriginal: {},
        savingQuick: false,

        /* ---- 参考模板校准 ---- */
        tplResult: null,
        tplLoading: false,
        tplApplying: false,
        tplSelected: {},
        tplOnlyChanged: true,
        tplProfileColumns: [
          { prop: 'font_eastasia', label: '中文字体' },
          { prop: 'font_ascii', label: '西文字体' },
          { prop: 'font_size', label: '字号' },
          { prop: 'line_spacing', label: '行距' },
          { prop: 'align', label: '对齐' },
          { prop: 'first_line_indent', label: '首行缩进' }
        ],

        dragging: false,
        uploading: false,
        showShortcuts: false,

        checkerSamples: {
          prop_equals: ['scope / prop / expected / tol'],
          prop_range: ['scope / prop / min / max / kind_prop'],
          prop_in_set: ['scope / prop / allowed'],
          consistency: ['scope / prop'],
          heading_no_skip: ['（无参数）'],
          heading_style_used: ['（无参数）'],
          numbering_continuous: ['prefix: 图 / 表'],
          caption_referenced: ['（无参数）'],
          required_section: ['names / positions'],
          length_range: ['target: abstract|keywords|reference|title / min / max'],
          punctuation_mix: ['（无参数）'],
          reference_format: ['（无参数）'],
          empty_heading: ['（无参数）'],
          paragraph_run_uniform: ['（无参数）'],
          blank_line_run: ['max_consecutive'],
          page_number_present: ['（无参数）']
        }
      };
    },

    computed: {
      /** 问题清单：按严重级别 / 维度筛选，并剔除用户已忽略的条目 */
      visibleIssues: function () {
        if (!this.report) return [];
        var self = this;
        return this.report.issues.filter(function (issue) {
          if (self.ignored[self.issueKey(issue)]) return false;
          if (self.sevFilter !== 'all') {
            if (self.sevFilter === 'unknown') {
              if (issue.status !== 'unknown') return false;
            } else if (issue.severity !== self.sevFilter || issue.status !== 'fail') {
              return false;
            }
          }
          if (self.catFilter !== 'all' && issue.category !== self.catFilter) return false;
          return true;
        });
      },

      ignoredCount: function () {
        var self = this;
        if (!this.report) return 0;
        return this.report.issues.filter(function (issue) {
          return !!self.ignored[self.issueKey(issue)];
        }).length;
      },

      /** 段落序号 → 最高级别的问题，用于原文对照的底色 */
      hitMap: function () {
        var map = {};
        if (!this.report) return map;
        this.report.issues.forEach(function (issue) {
          var index = issue.location ? issue.location.paragraph_index : null;
          if (index === null || index === undefined) return;
          var sev = issue.status === 'fail' ? issue.severity : 'unknown';
          if (!map[index] || SEVERITY_RANK[sev] < SEVERITY_RANK[map[index]]) {
            map[index] = sev;
          }
        });
        return map;
      },

      filteredRuleResults: function () {
        if (!this.report) return [];
        var filter = this.ruleResultFilter;
        return this.report.rule_results.filter(function (row) {
          if (filter === 'all') return true;
          return row.status === filter;
        });
      },

      filteredRules: function () {
        if (!this.ruleset) return [];
        var keyword = this.ruleKeyword.trim().toLowerCase();
        var category = this.ruleCat;
        return this.ruleset.rules.filter(function (rule) {
          if (category !== 'all' && rule.category !== category) return false;
          if (!keyword) return true;
          var haystack = [rule.id, rule.name, rule.suggestion, JSON.stringify(rule.params), rule.reference]
            .join(' ')
            .toLowerCase();
          return haystack.indexOf(keyword) >= 0;
        });
      },

      enabledRuleCount: function () {
        if (!this.ruleset) return 0;
        return this.ruleset.rules.filter(function (rule) { return rule.enabled; }).length;
      },

      /* ---- 雷达图几何：四个维度分别落在上 / 右 / 下 / 左四个方向 ---- */
      radarGeometry: function () {
        var scores = (this.report && this.report.category_scores) || [];
        var center = 100;
        var radius = 66;
        // 三角函数会带出 100.00000000000001 这类浮点噪声，统一收敛到 2 位小数，
        // 既让 SVG 坐标稳定可比，也避免下游断言/测试被噪声干扰。
        function r2(value) { return Math.round(value * 100) / 100; }
        var items = scores.map(function (item, index) {
          var angle = (-90 + index * 90) * Math.PI / 180;
          var ratio = item.score === null || item.score === undefined ? 0 : item.score / 100;
          return {
            label: item.label,
            angle: angle,
            cx: center,
            cy: center,
            x: r2(center + radius * Math.cos(angle)),
            y: r2(center + radius * Math.sin(angle)),
            px: r2(center + radius * ratio * Math.cos(angle)),
            py: r2(center + radius * ratio * Math.sin(angle)),
            tx: r2(center + (radius + 20) * Math.cos(angle)),
            ty: r2(center + (radius + 20) * Math.sin(angle))
          };
        });
        return { center: center, radius: radius, items: items };
      },

      radarAxes: function () { return this.radarGeometry.items; },

      radarDots: function () {
        return this.radarGeometry.items.map(function (item) {
          return { label: item.label, x: item.px, y: item.py };
        });
      },

      radarPoints: function () {
        return this.radarGeometry.items.map(function (item) { return item.px + ',' + item.py; }).join(' ');
      },

      radarMid: function () {
        var geo = this.radarGeometry;
        function r2(value) { return Math.round(value * 100) / 100; }
        return geo.items.map(function (item) {
          return r2(geo.center + geo.radius * 0.5 * Math.cos(item.angle)) + ',' +
                 r2(geo.center + geo.radius * 0.5 * Math.sin(item.angle));
        }).join(' ');
      },

      radarGrid: function () {
        return this.radarGeometry.items.map(function (item) { return item.x + ',' + item.y; }).join(' ');
      },

      /* ---- 快速调整：统计项数与「有未保存改动」的项数 ---- */

      quickItemCount: function () {
        var total = 0;
        this.quickGroups.forEach(function (group) { total += group.items.length; });
        return total;
      },

      quickDirtyCount: function () {
        var self = this;
        var count = 0;
        this.quickGroups.forEach(function (group) {
          group.items.forEach(function (item) {
            if (self.isQuickDirty(item.rule_id)) count += 1;
          });
        });
        return count;
      },

      /* ---- 参考模板：勾选数与当前要显示的行 ---- */

      tplSelectedCount: function () {
        var self = this;
        if (!this.tplResult) return 0;
        return this.tplResult.proposals.filter(function (row) {
          return row.status === 'proposed' && self.tplSelected[row.rule_id];
        }).length;
      },

      visibleTemplateProposals: function () {
        if (!this.tplResult) return [];
        var onlyChanged = this.tplOnlyChanged;
        return this.tplResult.proposals.filter(function (row) {
          if (onlyChanged) return row.status === 'proposed';
          // 不筛选时也隐掉「本来就没有阈值可调」的那一大批，那是噪音而不是信息
          return row.status !== 'not_applicable';
        });
      }
    },

    watch: {
      /* 规则库用 v-show 常驻在 DOM 里（为了保留表单状态），所以不会触发 mounted，
         必须在切进来的时候按需加载，否则用户会看到一个空白表格。 */
      view: function (value) {
        if (value === 'rules') this.ensureRulesLoaded();
      }
    },

    methods: {
      /* ---------------------------------------------------- 基础请求 */

      request: async function (path, options) {
        var response = await fetch(API + path, options || {});
        var text = await response.text();
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (error) { data = { message: text }; }
        if (!response.ok) {
          var err = new Error((data && data.message) || ('请求失败：HTTP ' + response.status));
          err.hint = data && data.hint;
          err.detail = data && data.detail;
          err.code = data && data.code;
          throw err;
        }
        return data;
      },

      toastError: function (error) {
        var message = error && error.message ? error.message : '未知错误';
        if (error && error.hint) message += '\n\n建议：' + error.hint;
        if (error && error.detail && error.detail.length) {
          message += '\n\n· ' + [].concat(error.detail).slice(0, 4).join('\n· ');
        }
        ElementPlus.ElMessage({ type: 'error', message: message, duration: 7000, showClose: true });
      },

      toastOk: function (text) {
        ElementPlus.ElMessage({ type: 'success', message: text, duration: 2200 });
      },

      /* ---------------------------------------------------- 初始化 */

      loadConfig: async function () {
        var config = await this.request('/config');
        this.config = config;
        this.expectedRuleCount = (config.ruleset && config.ruleset.rule_count) || 56;
      },

      loadHealth: async function () {
        this.health = await this.request('/health');
      },

      loadStats: async function () {
        this.stats = await this.request('/stats');
      },

      loadSamples: async function () {
        var data = await this.request('/samples');
        this.samples = data.items || [];
      },

      loadDocuments: async function () {
        var data = await this.request('/documents');
        this.documents = data.items || [];
        if (this.current) {
          var fresh = this.documents.filter(function (item) { return item.id === this.current.id; }.bind(this))[0];
          if (fresh) this.current = Object.assign({}, this.current, fresh);
        }
      },

      /* ---------------------------------------------------- 上传与样例 */

      pickFile: function () { this.$refs.fileInput.click(); },

      onFileChange: function (event) {
        var files = event.target.files;
        if (files && files.length) this.uploadFile(files[0]);
        event.target.value = '';
      },

      onDrop: function (event) {
        this.dragging = false;
        var files = event.dataTransfer && event.dataTransfer.files;
        if (files && files.length) this.uploadFile(files[0]);
      },

      validate: function (file) {
        var name = (file.name || '').toLowerCase();
        if (name.slice(-5) === '.docx') return null;
        if (name.slice(-4) === '.doc') {
          return '这是旧版 .doc 文件，暂不支持。请在 Word / WPS 里打开它，选择「另存为」→ 文件类型选「Word 文档 (*.docx)」，再上传转换后的文件。';
        }
        var ext = name.indexOf('.') >= 0 ? name.slice(name.lastIndexOf('.')) : '（无扩展名）';
        return '不支持的文件类型：' + ext + '。本系统目前只接受 .docx 格式。';
      },

      uploadFile: async function (file) {
        var problem = this.validate(file);
        if (problem) {
          ElementPlus.ElMessage({ type: 'warning', message: problem, duration: 8000, showClose: true });
          return;
        }
        var limit = (this.config.max_upload_mb || 20) * 1024 * 1024;
        if (file.size > limit) {
          ElementPlus.ElMessage({ type: 'warning', message: '文件 ' + (file.size / 1024 / 1024).toFixed(1) + ' MB，超过 ' + (this.config.max_upload_mb || 20) + ' MB 上限。' });
          return;
        }
        this.uploading = true;
        try {
          var form = new FormData();
          form.append('file', file);
          var record = await this.request('/documents', { method: 'POST', body: form });
          await this.loadDocuments();
          await this.selectDocument(record);
          this.toastOk(record.reused ? '这份文档之前已经上传过，已直接复用' : '解析完成，正在自动检查');
          await this.runCheck();
        } catch (error) {
          this.toastError(error);
        } finally {
          this.uploading = false;
        }
      },

      sampleLabel: function (name) {
        return name.replace(/\.docx$/i, '').replace(/-/g, ' ');
      },

      importSample: async function (item) {
        try {
          var record = await this.request('/samples/' + encodeURIComponent(item.name) + '/import', { method: 'POST' });
          await this.loadDocuments();
          await this.selectDocument(record);
          await this.runCheck();
        } catch (error) {
          this.toastError(error);
        }
      },

      /* ---------------------------------------------------- 文档与检查 */

      selectDocument: async function (item) {
        this.current = item;
        this.report = null;
        this.paragraphs = [];
        this.fixResult = null;
        this.tab = 'issues';
        this.currentIssueIndex = null;
        this.targetIndex = null;
        try {
          var paragraphs = await this.request('/documents/' + item.id + '/paragraphs?limit=800');
          this.paragraphs = paragraphs.items || [];
          this.report = await this.request('/documents/' + item.id + '/report');
        } catch (error) {
          this.report = null; // 还没检查过：404 是正常状态，不打扰用户
        }
      },

      runCheck: async function () {
        if (!this.current || this.checking) return;
        this.checking = true;
        this.tab = 'issues';
        this.feed = [];
        this.progress = 0;
        this.fixResult = null;
        this.currentIssueIndex = null;

        // 后端毫秒级返回，这里用「真实规则结果逐条揭示」的方式呈现检查过程：
        // 数据全部来自真实执行结果，只是把揭示节奏放慢，让等待变得可解释。
        var softTimer = setInterval(function () {
          if (this.progress < 90) this.progress += 1;
        }.bind(this), 26);

        try {
          var report = await this.request('/documents/' + this.current.id + '/check', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
          });
          clearInterval(softTimer);
          await this.revealRules(report.rule_results);
          this.report = report;
          this.progress = 100;
          this.toastOk('检查完成：' + report.score + ' 分（' + report.grade_label + '）');
          await Promise.all([this.loadDocuments(), this.loadHealth(), this.loadStats()]);
        } catch (error) {
          clearInterval(softTimer);
          this.toastError(error);
        } finally {
          this.checking = false;
        }
      },

      revealRules: function (rows) {
        var self = this;
        return new Promise(function (resolve) {
          var index = 0;
          var step = Math.max(8, Math.min(26, Math.round(1200 / Math.max(rows.length, 1))));
          var timer = setInterval(function () {
            if (index >= rows.length) {
              clearInterval(timer);
              resolve();
              return;
            }
            var row = rows[index];
            self.feed.push({
              rule_id: row.rule_id,
              name: row.rule_name,
              status: row.status,
              fail_count: row.fail_count
            });
            index += 1;
            self.progress = Math.round((index / rows.length) * 100);
          }, step);
        });
      },

      removeDocument: async function () {
        if (!this.current) return;
        try {
          await ElementPlus.ElMessageBox.confirm(
            '删除「' + this.current.filename + '」的检查记录和上传的文件？此操作不可撤销。',
            '确认删除',
            { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
          );
        } catch (error) {
          return;
        }
        try {
          await this.request('/documents/' + this.current.id, { method: 'DELETE' });
          this.current = null;
          this.report = null;
          this.paragraphs = [];
          await this.loadDocuments();
          this.toastOk('已删除');
        } catch (error) {
          this.toastError(error);
        }
      },

      /* ---------------------------------------------------- 问题展示 */

      issueKey: function (issue) {
        var index = issue.location ? issue.location.paragraph_index : null;
        return issue.rule_id + '#' + (index === null || index === undefined ? 'doc' : index);
      },

      locationText: function (issue) {
        var location = issue.location || {};
        var parts = [];
        if (location.section) parts.push(location.section);
        if (location.paragraph_index !== null && location.paragraph_index !== undefined) {
          parts.push('第 ' + (location.paragraph_index + 1) + ' 段');
        }
        if (location.in_table) parts.push('表格内');
        if (location.affected_count) parts.push(location.affected_count + ' 处');
        return parts.length ? parts.join(' · ') : '全文';
      },

      hitClass: function (index) {
        var hit = this.hitMap[index];
        return hit ? 'hit-' + hit : '';
      },

      locate: function (issue) {
        var index = issue.location ? issue.location.paragraph_index : null;
        if (index === null || index === undefined) return;
        this.currentIssueIndex = this.visibleIssues.indexOf(issue);
        this.tab = 'doc';
        this.targetIndex = index;
        this.$nextTick(function () {
          var element = document.getElementById('para-' + index);
          if (element) element.scrollIntoView({ block: 'center', behavior: 'smooth' });
        });
      },

      ignoreIssue: function (issue) {
        this.ignored[this.issueKey(issue)] = true;
        writeIgnored(this.ignored);
        this.ignored = Object.assign({}, this.ignored);
        this.toastOk('已忽略这条，刷新页面后仍然生效（可在顶部「全部恢复」）');
      },

      restoreIgnored: function () {
        this.ignored = {};
        writeIgnored({});
        this.toastOk('已恢复全部忽略项');
      },

      scoreClass: function (score) {
        if (score === null || score === undefined) return 'none';
        if (score >= 90) return 'good';
        if (score >= 75) return 'mid';
        if (score >= 60) return 'low';
        return 'bad';
      },

      ruleStatusType: function (status) {
        return { pass: 'success', fail: 'danger', unknown: 'warning', disabled: 'info' }[status] || 'info';
      },

      ruleStatusText: function (status) {
        return { pass: '通过', fail: '未通过', unknown: '待人工确认', disabled: '已停用' }[status] || status;
      },

      actionLabel: function (id) {
        var hit = (this.config.fix_actions || []).filter(function (item) { return item.id === id; })[0];
        return hit ? hit.label : id;
      },

      topIssueWidth: function (item) {
        var top = this.stats && this.stats.top_issues && this.stats.top_issues.length
          ? this.stats.top_issues[0].count
          : 1;
        return Math.max(6, Math.round((item.count / top) * 100));
      },

      /* ---------------------------------------------------- 导出与修复 */

      copyMarkdown: async function () {
        if (!this.current) return;
        try {
          var response = await fetch(API + '/documents/' + this.current.id + '/export?kind=markdown');
          var text = await response.text();
          if (navigator.clipboard && window.isSecureContext !== false) {
            await navigator.clipboard.writeText(text);
          } else {
            var area = document.createElement('textarea');
            area.value = text;
            document.body.appendChild(area);
            area.select();
            document.execCommand('copy');
            document.body.removeChild(area);
          }
          this.toastOk('整改清单已复制，可以直接粘贴到群里');
        } catch (error) {
          this.toastError(error);
        }
      },

      exportReport: function (kind) {
        if (!this.current) return;
        window.open(API + '/documents/' + this.current.id + '/export?kind=' + kind, '_blank');
      },

      runFix: async function () {
        if (!this.current || !this.fixActions.length) return;
        this.fixing = true;
        try {
          this.fixResult = await this.request('/documents/' + this.current.id + '/fix', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ actions: this.fixActions })
          });
          this.toastOk('已生成规范化文件，原文件未被修改');
        } catch (error) {
          this.toastError(error);
        } finally {
          this.fixing = false;
        }
      },

      downloadFixed: function () {
        if (!this.fixResult) return;
        window.open(API + '/fixed/' + this.fixResult.fixed_id + '/download', '_blank');
      },

      /* ---------------------------------------------------- 规则库 */

      loadRuleset: async function () {
        this.ruleset = await this.request('/rules/default');
        var response = await fetch(API + '/rules/default/yaml');
        this.yamlText = await response.text();
        this.yamlMessage = '';
        // 规则集变了，快速调整页上的数字与原始值也要同步，否则会显示成过期值
        await this.loadQuick();
      },

      saveYaml: async function () {
        this.savingYaml = true;
        try {
          var result = await this.request('/rules/default', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ yaml_text: this.yamlText })
          });
          this.yamlMessage = '已保存 · ' + result.ruleset.rule_count + ' 条规则 · ' + new Date().toLocaleTimeString();
          this.toastOk('规则集已保存并生效');
          await this.loadRuleset();
          await this.loadConfig();
        } catch (error) {
          this.yamlMessage = '';
          this.toastError(error);
        } finally {
          this.savingYaml = false;
        }
      },

      toggleRule: async function (rule, value) {
        try {
          var result = await this.request('/rules/default/toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rule_id: rule.id, enabled: value })
          });
          this.ruleset = result.ruleset;
          this.toastOk((value ? '已启用：' : '已停用：') + rule.name);
          await this.loadConfig();
          await this.loadQuick();
        } catch (error) {
          this.toastError(error);
          await this.loadRuleset();
        }
      },

      resetRuleset: async function () {
        try {
          await ElementPlus.ElMessageBox.confirm(
            '恢复后你在界面上做过的所有规则调整都会丢失，规则集将回到内置的 56 条。',
            '恢复内置默认规则集',
            { type: 'warning', confirmButtonText: '恢复', cancelButtonText: '取消' }
          );
        } catch (error) {
          return;
        }
        try {
          await this.request('/rules/default/reset', { method: 'POST' });
          await this.loadRuleset();
          await this.loadConfig();
          this.toastOk('已恢复内置默认规则集');
        } catch (error) {
          this.toastError(error);
        }
      },

      /* ---------------------------------------------------- 阈值快速调整 */

      /** 进入规则库时按需加载：规则详情 + 可快速调整的阈值分组 */
      ensureRulesLoaded: async function () {
        try {
          if (!this.ruleset) await this.loadRuleset();
          if (!this.quickGroups.length) await this.loadQuick();
        } catch (error) {
          this.toastError(error);
        }
      },

      loadQuick: async function () {
        var self = this;
        var data = await this.request('/rules/default/quick');
        var values = {};
        var original = {};
        (data.groups || []).forEach(function (group) {
          group.items.forEach(function (item) {
            var snapshot = self.quickSnapshot(item);
            // 原始值单独深拷贝一份：界面上改的是 values，用来和 original 对比算出改动项
            values[item.rule_id] = snapshot;
            original[item.rule_id] = JSON.parse(JSON.stringify(snapshot));
          });
        });
        this.quickGroups = data.groups || [];
        this.quickValues = values;
        this.quickOriginal = original;
      },

      /** 把一条规则翻译成表单值；不同 kind 的可编辑字段不一样 */
      quickSnapshot: function (item) {
        if (item.kind === 'number') return { expected: item.value, tol: item.tol };
        if (item.kind === 'range') return { min: item.min, max: item.max };
        if (item.kind === 'enum') return { expected: item.value };
        return { allowed: (item.value || []).slice() };
      },

      isQuickDirty: function (ruleId) {
        var current = this.quickValues[ruleId];
        var original = this.quickOriginal[ruleId];
        if (!current || !original) return false;
        return JSON.stringify(current) !== JSON.stringify(original);
      },

      discardQuick: function () {
        var self = this;
        var values = {};
        Object.keys(this.quickOriginal).forEach(function (ruleId) {
          values[ruleId] = JSON.parse(JSON.stringify(self.quickOriginal[ruleId]));
        });
        this.quickValues = values;
        this.toastOk('已放弃未保存的修改');
      },

      collectQuickUpdates: function () {
        var self = this;
        var updates = [];
        this.quickGroups.forEach(function (group) {
          group.items.forEach(function (item) {
            if (!self.isQuickDirty(item.rule_id)) return;
            var current = self.quickValues[item.rule_id];
            var params = {};
            if (item.kind === 'number') {
              params.expected = current.expected;
              params.tol = current.tol;
            } else if (item.kind === 'range') {
              params.min = current.min;
              params.max = current.max;
            } else if (item.kind === 'enum') {
              params.expected = current.expected;
            } else {
              params.allowed = current.allowed;
            }
            updates.push({ rule_id: item.rule_id, params: params });
          });
        });
        return updates;
      },

      saveQuick: async function () {
        var updates = this.collectQuickUpdates();
        if (!updates.length) return;
        this.savingQuick = true;
        try {
          var result = await this.request('/rules/default/quick', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ updates: updates })
          });
          if (result.rejected) this.reportRejected(result.problems, result.rejected);
          if (result.applied) this.toastOk('已保存 ' + result.applied + ' 项阈值调整，立即对新检查生效');
          await this.loadRuleset();
          await this.loadConfig();
        } catch (error) {
          this.toastError(error);
        } finally {
          this.savingQuick = false;
        }
      },

      /** 批量写回时可能「部分成功」：把没成功的逐条说清楚，而不是笼统报一句失败 */
      reportRejected: function (problems, count) {
        var lines = (problems || []).map(function (item) {
          return '· ' + (item.rule_name || item.rule_id) + ' —— ' + item.message + (item.hint ? '（' + item.hint + '）' : '');
        });
        ElementPlus.ElMessage({
          type: 'warning',
          duration: 10000,
          showClose: true,
          message: '有 ' + count + ' 项没有保存：\n' + lines.slice(0, 4).join('\n')
        });
      },

      /* ---------------------------------------------------- 参考模板校准 */

      pickTemplate: function () {
        this.$refs.tplInput.click();
      },

      onTemplateFile: function (event) {
        var files = event.target.files;
        if (files && files.length) this.profileTemplate(files[0]);
        event.target.value = '';
      },

      profileTemplate: async function (file) {
        if (!/\.docx$/i.test(file.name || '')) {
          ElementPlus.ElMessage({
            type: 'warning',
            duration: 8000,
            showClose: true,
            message: '参考模板只支持 .docx 格式。旧版 .doc 请先用 Word / WPS 打开，选择「另存为」→「Word 文档 (*.docx)」后再上传。'
          });
          return;
        }
        this.tplLoading = true;
        try {
          var form = new FormData();
          form.append('file', file);
          var data = await this.request('/templates/profile?ruleset_id=default', { method: 'POST', body: form });
          this.tplResult = data;
          this.resetTemplateSelection(data.proposals);
          this.ruleTab = 'template';
          this.toastOk('模板已量完：' + data.counts.proposed + ' 项阈值建议调整，' + data.counts.unchanged + ' 项已一致');
        } catch (error) {
          this.toastError(error);
        } finally {
          this.tplLoading = false;
        }
      },

      resetTemplateSelection: function (proposals) {
        var selected = {};
        (proposals || []).forEach(function (row) {
          selected[row.rule_id] = row.status === 'proposed';
        });
        this.tplSelected = selected;
      },

      selectAllTemplateProposals: function () {
        if (this.tplResult) this.resetTemplateSelection(this.tplResult.proposals);
      },

      clearTemplate: function () {
        this.tplResult = null;
        this.tplSelected = {};
        this.tplOnlyChanged = true;
      },

      tplCell: function (role, prop) {
        var hit = (role.cells || []).filter(function (cell) { return cell.prop === prop; })[0];
        if (!hit) return '—';
        if (hit.consistency >= 1) return hit.display;
        return hit.display + '（' + Math.round(hit.consistency * 100) + '%）';
      },

      applyTemplate: async function () {
        var self = this;
        if (!this.tplResult) return;
        var updates = [];
        this.tplResult.proposals.forEach(function (row) {
          if (row.status === 'proposed' && self.tplSelected[row.rule_id] && row.proposal) {
            updates.push({ rule_id: row.rule_id, params: row.proposal });
          }
        });
        if (!updates.length) {
          ElementPlus.ElMessage({ type: 'warning', message: '还没有勾选任何要应用的项。' });
          return;
        }
        try {
          await ElementPlus.ElMessageBox.confirm(
            '将把 ' + updates.length + ' 条规则的阈值改为模板实测值，保存后立即对新检查生效。\n如需整体回退，可点右上角「恢复内置默认」。',
            '应用模板推导结果',
            { type: 'warning', confirmButtonText: '应用', cancelButtonText: '取消' }
          );
        } catch (error) {
          return;
        }
        this.tplApplying = true;
        try {
          var result = await this.request('/templates/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ruleset_id: 'default', source_name: this.tplResult.filename, updates: updates })
          });
          if (result.rejected) this.reportRejected(result.problems, result.rejected);
          if (result.applied) this.toastOk('已按模板校准 ' + result.applied + ' 项规则阈值');
          await this.loadRuleset();
          await this.loadConfig();
          // 再推导一次，让界面变成「哪些已经一致了」，而不是停在过期结论上
          try {
            var refreshed = await this.request(
              '/templates/' + this.tplResult.template_id + '/derive?ruleset_id=default',
              { method: 'POST' }
            );
            this.tplResult = refreshed;
            this.resetTemplateSelection(refreshed.proposals);
          } catch (error) {
            /* 重新推导失败不影响已经保存成功的结果 */
          }
        } catch (error) {
          this.toastError(error);
        } finally {
          this.tplApplying = false;
        }
      },

      /* ---------------------------------------------------- 键盘 */

      onKey: function (event) {
        var key = event.key;
        if (event.ctrlKey && key === 'Enter') {
          event.preventDefault();
          this.runCheck();
          return;
        }
        if (event.ctrlKey && (key === 's' || key === 'S') && this.view === 'rules') {
          event.preventDefault();
          this.saveYaml();
          return;
        }
        if (key === 'Escape') {
          if (this.showShortcuts) { this.showShortcuts = false; return; }
          if (this.tab === 'doc') this.tab = 'issues';
          return;
        }
        if ((key === 'ArrowDown' || key === 'ArrowUp') && this.view === 'check' && this.visibleIssues.length) {
          event.preventDefault();
          var index = this.currentIssueIndex === null ? -1 : this.currentIssueIndex;
          index += key === 'ArrowDown' ? 1 : -1;
          if (index < 0) index = this.visibleIssues.length - 1;
          if (index >= this.visibleIssues.length) index = 0;
          this.locate(this.visibleIssues[index]);
        }
      }
    },

    mounted: async function () {
      window.addEventListener('keydown', this.onKey);
      try {
        await this.loadConfig();
      } catch (error) {
        this.toastError(error);
      }
      try {
        await Promise.all([this.loadHealth(), this.loadSamples(), this.loadDocuments(), this.loadStats()]);
      } catch (error) {
        /* 单个接口失败不影响主流程，界面上会有可见的空状态 */
      }
      // 打开页面就显示最近一份文档的报告，省掉一次点击（也让自动化检查能直接断言报告区）
      if (!this.current && this.documents.length) {
        try {
          await this.selectDocument(this.documents[0]);
        } catch (error) {
          /* 读不到就保持空状态 */
        }
      }
    }
  });

  if (window.ElementPlusLocaleZhCn) {
    app.use(ElementPlus, { locale: window.ElementPlusLocaleZhCn });
  } else {
    app.use(ElementPlus);
  }
  // 挂载句柄暴露到 window，便于调试与自动化自检（不参与任何业务逻辑）
  window.__gezhi = app.mount('#app');
})();
