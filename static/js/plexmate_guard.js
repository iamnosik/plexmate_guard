function guardAjax(moduleName, command, data, callback) {
  $.ajax({
    url: '/' + PLEXMATE_GUARD_PACKAGE + '/ajax/' + moduleName + '/' + command,
    type: 'POST', cache: false, data: data || {}, dataType: 'json',
    success: function(ret) { if (ret.msg) notify(ret.msg, ret.ret || 'info'); if (callback) callback(ret); },
    error: function(xhr) { notify((xhr.responseJSON && xhr.responseJSON.msg) || 'Guard 요청 처리에 실패했습니다.', 'danger'); }
  });
}
