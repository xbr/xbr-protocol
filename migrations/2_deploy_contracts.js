///////////////////////////////////////////////////////////////////////////////
//
//  XBR Open Data Markets - https://xbr.network
//
//  JavaScript client library for the XBR Network.
//
//  Copyright (C) Crossbar.io Technologies GmbH and contributors
//
//  Licensed under the Apache 2.0 License:
//  https://opensource.org/licenses/Apache-2.0
//
///////////////////////////////////////////////////////////////////////////////

var XBRToken = artifacts.require("./XBRToken.sol");
var XBRNetwork = artifacts.require("./XBRNetwork.sol");
// var XBRPaymentChannel = artifacts.require("./XBRPaymentChannel.sol");
// var XBRNetworkProxy = artifacts.require("./XBRNetworkProxy.sol");

// https://truffleframework.com/docs/truffle/getting-started/running-migrations#deployer
module.exports = function (deployer, network, accounts) {

    // const organization = "0x0000000000000000000000000000000000000000";
    const organization = accounts[0];

    // Deploy XBRToken, then deploy XBRNetwork, passing in XBRToken's newly deployed address
    deployer.deploy(XBRToken).then(function() {
        return deployer.deploy(XBRNetwork, XBRToken.address, organization);
    });

    // deployer.deploy(XBRPaymentChannel);
    // deployer.deploy(XBRNetworkProxy);
};
