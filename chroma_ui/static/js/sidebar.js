


var Sidebar = function(){
  var initialized = false;

  function eventStyle(ev)
  {
    var cssClassName = "";
    if(ev.severity == 'ERROR') //red
      cssClassName='palered';
    else if(ev.severity == 'INFO') //normal
      cssClassName='';
    else if(ev.severity == 'WARNING') //yellow
      cssClassName='brightyellow';
    
    return cssClassName;
  }

  function eventIcon(e)
  {
    return "/static/images/" + {
      INFO: 'dialog-information.png',
      ERROR: 'dialog-error.png',
      WARNING: 'dialog-warning.png',
    }[e.severity]
  }

  function jobIcon(job)
  {
    var prefix = "/static/images/";
    if(job.state == 'complete') {
      if (job.errored) {
        return prefix + "dialog-error.png"
      } else if (job.cancelled) {
        return prefix + "gtk-cancel.png"
      } else {
        return prefix + "dialog_correct.png"
      }
    } else if (job.state == 'paused') {
      return prefix + "gtk-media-pause.png"
    } else {
      return prefix + "ajax-loader.gif"
    }
  }

  function commandIcon(command)
  {
    var prefix = "/static/images/";
    if(!command.complete) {
      return prefix + "ajax-loader.gif"
    } else if (command.errored) {
        return prefix + "dialog-error.png"
    } else if (command.cancelled) {
        return prefix + "gtk-cancel.png"
    } else {
        return prefix + "dialog_correct.png"
    }
  }

  function alertIcon(a)
  {
    return "/static/images/dialog-warning.png";
  }

  function ellipsize(str)
  {
    /* FIXME: find or implement real ellipsization by element size
     * rather than arbitrary string length limit */
    var length = 40;
    if (str.length > (length - 3)) {
      return str.substr(0, length) + "..."
    } else {
      return str
    }
  }

  function init() {
    $("div#sidebar div#accordion").accordion({
      fillSpace: true,
      collapsible: true,
      changestart: function (event, ui) {
        var active = $('div#sidebar div#accordion').accordion("option", "active");
        if (active == 0) {
          $('div.leftpanel table#alerts').dataTable().fnDraw();
        } else if (active == 1) {
          $('div.leftpanel table#events').dataTable().fnDraw();
        } else if (active == 2) {
          $('div.leftpanel table.commands').dataTable().fnDraw();
        } else {
          throw "Unknown accordion index " + active
        }
      }
    });

    smallTable($('div.leftpanel table.commands'), 'command/',
      {order_by: "-created_at"},
      function(command) {
        command.icon = "<img src='" + commandIcon(command) + "'/>"
        // TODO: cancelling jobs within commands (and commands themselves?)
        command.text = ellipsize(command.message) + "<br>" + shortLocalTime(command.created_at)
        command.buttons = "<a class='navigation' href='/ui/command/" + command.id + "/'>Open</a>";
      },
      [
        { "sClass": 'icon_column', "mDataProp":"icon", bSortable: false },
        { "sClass": 'txtleft', "mDataProp":"text", bSortable: false },
        { "sClass": 'txtleft', 'mDataProp': 'buttons', bSortable: false },
      ]
    );

    smallTable($('div.leftpanel table#alerts'), 'alert/',
      {active: true, order_by: "-begin"},
      function(a) {
        a.text = ellipsize(a.message) + "<br>" + shortLocalTime(a.begin)
        a.icon = "<img src='" + alertIcon(a) + "'/>"
      },
      [
        { "sClass": 'icon_column', "mDataProp":"icon", bSortable: false },
        { "sClass": 'txtleft', "mDataProp":"text", bSortable: false },
      ],
      "<img src='/static/images/dialog_correct.png'/>&nbsp;No alerts active"
    );

    smallTable($('div.leftpanel table#events'), 'event/',
      {order_by: "-created_at"},
      function(e) {
        e.icon = "<img src='" + eventIcon(e) + "'/>"
        e.DT_RowClass = eventStyle(e)
        e.text = ellipsize(e.message) + "<br>" + shortLocalTime(e.created_at)
      },
      [
        { "sClass": 'icon_column', "mDataProp": "icon", bSortable: false },
        { "sClass": 'txtleft', "mDataProp": "text", bSortable: false },
      ]
    );

    initialized = true;
  }

  function smallTable(element, url, kwargs, row_fn, columns, emptyText) {
    element.dataTable({
        bProcessing: true,
        bServerSide: true,
        iDisplayLength:10,
        bDeferRender: true,
        sAjaxSource: url,
        fnServerData: function (url, data, callback, settings) {
          Api.get_datatables(url, data, function(data){
            $.each(data.aaData, function(i, row) {
              row_fn(row);
            });
            callback(data);
          }, settings, kwargs);
        },
        aoColumns: columns,
        oLanguage: {
          "sProcessing": "<img src='/static/images/loading.gif' style='margin-top:10px;margin-bottom:10px' width='16' height='16' />",
          sZeroRecords: emptyText
        },
        bJQueryUI: true,
        bFilter: false
      });
    // Hide the header
    element.prev().hide();
    element.find('thead').hide();

    // Hide the "x of y" text from the footer
    element.next().find('.dataTables_info').hide();
  }

  function open() {
    if (!initialized) {
      init();
    }
    $("div#sidebar div#accordion").change();
    $("#sidebar").show({effect: 'slide'});
  }

  function close() {
    $("#sidebar").hide({effect: 'slide'});
  }

  return {
    open: open,
    close: close
  }
}();

/* FIXME: global function because of the way it's called from an onclick */
setJobState = function(job_id, state)
{
  Api.put("job/" + job_id + "/", {'state': state},
  success_callback = function(data)
  {
    $('div.leftpanel table#jobs').dataTable().fnDraw();
  });
}

/* FIXME: move this somewhere sensible */
loadHostList = function(filesystem_id, targetContainer)
{
  var hostList = '<option value="">All</option>';
  
  var api_params = {'filesystem_id':filesystem_id};

  Api.get("host/", api_params,
  success_callback = function(data)
  {
    $.each(data.objects, function(i, host)
    {
      hostList  =  hostList + "<option value="+host.id+">"+host.label+"</option>";
    });
    $('#'+targetContainer).html(hostList);
  });
}


$(document).ready(function() 
{
  $("#sidebar_open").click(function()
  {
    Sidebar.open();
    return false;
  });

  $("#sidebar_close").button({icons:{primary:'ui-icon-close'}});
  $("#sidebar_close").click(function()
  {
    Sidebar.close();
    return false;
  });
});


