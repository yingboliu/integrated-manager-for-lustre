//
// INTEL CONFIDENTIAL
//
// Copyright 2013 Intel Corporation All Rights Reserved.
//
// The source code contained or described herein and all documents related
// to the source code ("Material") are owned by Intel Corporation or its
// suppliers or licensors. Title to the Material remains with Intel Corporation
// or its suppliers and licensors. The Material contains trade secrets and
// proprietary and confidential information of Intel or its suppliers and
// licensors. The Material is protected by worldwide copyright and trade secret
// laws and treaty provisions. No part of the Material may be used, copied,
// reproduced, modified, published, uploaded, posted, transmitted, distributed,
// or disclosed in any way without Intel's prior express written permission.
//
// No license under any patent, copyright, trade secret or other intellectual
// property right is granted to or conferred upon you by disclosure or delivery
// of the Materials, either expressly, by implication, inducement, estoppel or
// otherwise. Any license under such intellectual property rights must be
// express and approved by Intel in writing.


'use strict';

var inherits = require('util').inherits,
  url = require('url'),
  _ = require('lodash');

module.exports = function mdtDataSourceFactory(MetricsDataSource) {
  function MdtDataSource(name) {
    MetricsDataSource.call(this, name);
  }

  inherits(MdtDataSource, MetricsDataSource);

  MdtDataSource.prototype.beforeSend = function (options) {
    var opts = MdtDataSource.super_.prototype.beforeSend.call(this, options);

    return _.merge({
      url: url.resolve(this.url, 'target/metric/'),
      qs: {
        reduce_fn: 'sum',
        kind: 'MDT',
        metrics: 'stats_close,stats_getattr,stats_getxattr,stats_link,stats_mkdir,stats_mknod,stats_open,\
stats_rename,stats_rmdir,stats_setattr,stats_statfs,stats_unlink'
      }
    }, opts);
  };

  return function getInstance(name) {
    return new MdtDataSource(name);
  };
};