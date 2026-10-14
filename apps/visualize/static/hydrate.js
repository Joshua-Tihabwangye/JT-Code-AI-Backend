/* JT-Code chart hydration for exported reports (mirrors the web app's src/features/visualize/hydrate.ts).
 * Chart options are plain JSON built by the server; the few places ECharts needs a function are
 * written as {"$fn": "<name>", ...} markers and replaced here by a fixed set of functions. */
(function (root) {
  'use strict';
  function fmt(value, digits) {
    if (typeof value !== 'number' || !isFinite(value)) return value == null ? '' : String(value);
    return value.toLocaleString(undefined, { maximumFractionDigits: digits == null ? 4 : digits });
  }
  function compact(value) {
    if (typeof value !== 'number' || !isFinite(value)) return String(value);
    var magnitude = Math.abs(value);
    var units = [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']];
    for (var i = 0; i < units.length; i += 1) {
      if (magnitude >= units[i][0]) return (value / units[i][0]).toFixed(1).replace(/\.0$/, '') + units[i][1];
    }
    return magnitude < 1 && magnitude > 0 ? value.toPrecision(2) : fmt(value, 2);
  }
  function shapes(series) {
    return function (params, api) {
      var item = series.data[params.dataIndex] || {};
      var children = (item.shapes || []).map(function (shape) {
        var points = (shape.points || []).map(function (point) {
          var at = api.coord([point[0], point[1]]);
          return [at[0] + (point[2] || 0), at[1] + (point[3] || 0)];
        });
        var colour = shape.color || api.visual('color');
        if (shape.type === 'text') {
          return { type: 'text', x: points[0][0], y: points[0][1], silent: true,
            style: { text: shape.text, fill: shape.textColor || '#fff', font: shape.font || '600 11px sans-serif', align: 'center', verticalAlign: 'middle' } };
        }
        if (shape.type === 'polyline') {
          return { type: 'polyline', shape: { points: points }, style: { stroke: colour, lineWidth: shape.width || 1.5, fill: null } };
        }
        return { type: 'polygon', shape: { points: points },
          style: { fill: colour, opacity: shape.opacity == null ? 0.85 : shape.opacity, stroke: shape.stroke || null, lineWidth: shape.strokeWidth || 0 } };
      });
      return { type: 'group', children: children };
    };
  }
  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }
  function tableHtml(table) {
    var head = (table.columns || []).map(function (c) { return '<th style="text-align:left;padding:6px 10px;border-bottom:2px solid #6366f1;position:sticky;top:0;background:inherit">' + escapeHtml(c) + '</th>'; }).join('');
    var body = (table.rows || []).map(function (row, i) {
      return '<tr style="background:' + (i % 2 ? 'rgba(99,102,241,.06)' : 'transparent') + '">' + row.map(function (cell) {
        var text = typeof cell === 'number' ? fmt(cell) : escapeHtml(cell);
        return '<td style="padding:5px 10px;border-bottom:1px solid rgba(148,163,184,.25);' + (typeof cell === 'number' ? 'text-align:right' : '') + '">' + text + '</td>';
      }).join('') + '</tr>';
    }).join('');
    var note = table.truncated ? '<p style="font:12px sans-serif;opacity:.7">Showing the first ' + table.rows.length + ' rows.</p>' : '';
    return '<div style="max-height:100%;overflow:auto;font:13px Inter,system-ui,sans-serif"><table style="border-collapse:collapse;width:100%">' +
      '<thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table>' + note + '</div>';
  }
  var factories = {
    dataTable: function (spec) { return function () { return tableHtml(spec.table || {}); }; },
    number: function () { return function (value) { return fmt(value); }; },
    compact: function () { return compact; },
    percent: function (spec) {
      return function (value) { return typeof value === 'number' ? (value * (spec.scale || 1)).toFixed(spec.digits == null ? 1 : spec.digits) + '%' : value; };
    },
    indexLabel: function (spec) {
      return function (value) { return Math.abs(value - Math.round(value)) < 1e-6 ? (spec.labels[Math.round(value)] || '') : ''; };
    },
    cellValue: function () { return function (params) { var v = params.value; return compact(Array.isArray(v) ? v[2] : v); }; },
    labelValue: function (spec) {
      return function (params) { var v = Array.isArray(params.value) ? params.value[1] : params.value; return v == null ? '' : spec.percent ? fmt(v, 1) + '%' : compact(v); };
    },
    shapeTooltip: function () {
      return function (params) { var v = params.value; var size = Array.isArray(v) && v.length > 2 ? ': ' + fmt(v[2]) : ''; return (params.marker || '') + (params.name || params.seriesName) + size; };
    },
  };
  function hydrate(value, series) {
    if (Array.isArray(value)) return value.map(function (item) { return hydrate(item, series); });
    if (!value || typeof value !== 'object') return value;
    if (typeof value.$fn === 'string') {
      if (value.$fn === 'shapes') return shapes(series);
      var factory = factories[value.$fn];
      return factory ? factory(value) : undefined;
    }
    var out = {};
    Object.keys(value).forEach(function (key) {
      var isSeries = key === 'series';
      var child = value[key];
      if (isSeries && Array.isArray(child)) {
        out[key] = child.map(function (entry) { return hydrate(entry, entry); });
      } else {
        out[key] = hydrate(child, series);
      }
    });
    return out;
  }
  root.jtHydrate = function (option) { return hydrate(option, null); };
})(typeof window !== 'undefined' ? window : this);
