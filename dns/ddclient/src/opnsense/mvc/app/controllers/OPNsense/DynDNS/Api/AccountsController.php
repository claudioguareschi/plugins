<?php

/**
 *    Copyright (C) 2021 Deciso B.V.
 *
 *    All rights reserved.
 *
 *    Redistribution and use in source and binary forms, with or without
 *    modification, are permitted provided that the following conditions are met:
 *
 *    1. Redistributions of source code must retain the above copyright notice,
 *       this list of conditions and the following disclaimer.
 *
 *    2. Redistributions in binary form must reproduce the above copyright
 *       notice, this list of conditions and the following disclaimer in the
 *       documentation and/or other materials provided with the distribution.
 *
 *    THIS SOFTWARE IS PROVIDED ``AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES,
 *    INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY
 *    AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
 *    AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY,
 *    OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
 *    SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
 *    INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
 *    CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
 *    ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 *    POSSIBILITY OF SUCH DAMAGE.
 *
 */

namespace OPNsense\DynDNS\Api;

use OPNsense\Base\ApiMutableModelControllerBase;
use OPNsense\Core\Backend;

class AccountsController extends ApiMutableModelControllerBase
{
    protected static $internalModelName = 'account';
    protected static $internalModelClass = 'OPNsense\DynDNS\DynDNS';

    public function searchItemAction()
    {
        $result = $this->searchBase(
            "accounts.account",
            [
              'enabled', 'service', 'description', 'username', 'hostnames', 'use_interface',
              'interface', 'protocol', 'current_ip', 'current_mtime'
            ],
            "description"
        );
        foreach ($result['rows'] as &$row) {
            if ($row['service'] == 'Custom') {
                $row['service'] = 'Custom (' . $row['protocol'] . ')';
            }
            unset($row['protocol']);
        }
        return $result;
    }

    public function setItemAction($uuid)
    {
        return $this->setBase("account", "accounts.account", $uuid);
    }

    public function addItemAction()
    {
        return $this->addBase("account", "accounts.account");
    }

    public function getItemAction($uuid = null)
    {
        return $this->getBase("account", "accounts.account", $uuid);
    }

    public function delItemAction($uuid)
    {
        return $this->delBase("accounts.account", $uuid);
    }

    public function toggleItemAction($uuid, $enabled = null)
    {
        return $this->toggleBase("accounts.account", $uuid, $enabled);
    }

    /**
     * Detect the current address and push it to the service, even when it equals the cached one.
     * @param string $uuids comma separated list of account uuids
     * @return array status (ok, failed or error) and the result per account (ok, failed or error)
     */
    public function forceRefreshAction($uuids = null)
    {
        $result = ['status' => 'failed'];
        if (!$this->request->isPost()) {
            return $result;
        }

        $uuids = array_values(array_unique(array_filter(array_map('trim', explode(',', $uuids ?? '')))));
        if (empty($uuids)) {
            $result['message'] = gettext('No accounts selected.');
            return $result;
        }
        foreach ($uuids as $uuid) {
            if (!preg_match('/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i', $uuid)) {
                $result['message'] = gettext('Invalid account.');
                return $result;
            }
        }

        $messages = [
            'unknown_account' => gettext('Account not found.'),
            'disabled' => gettext('Account is disabled.'),
            'not_applied' => gettext('Account is not active, apply the configuration first.'),
            'not_running' => gettext('The Dynamic DNS service is not running.'),
            'no_address' => gettext('No global IP address detected.'),
            'update_failed' => gettext('Update rejected or failed, see the log for details.'),
            'timeout' => gettext('No result received in time, see the log for details.'),
        ];
        $unexpected = gettext('Unexpected error, see the log for details.');

        $response = (new Backend())->configdpRun('ddclient force_refresh', [implode(',', $uuids)]);
        $response = json_decode($response ?? '', true);
        if (!is_array($response)) {
            $result['status'] = 'error';
            $result['message'] = $unexpected;
            return $result;
        }

        $mdl = $this->getModel();
        $result['status'] = 'ok';
        foreach ($uuids as $uuid) {
            $outcome = is_array($response[$uuid] ?? null) ? $response[$uuid] : [];
            $node = $mdl->getNodeByReference('accounts.account.' . $uuid);
            $account = ['description' => $uuid];
            if ($node != null) {
                $account['description'] = (string)$node->description ?: (string)$node->hostnames;
            }
            if (($outcome['status'] ?? '') == 'ok') {
                $account['status'] = 'ok';
                $account['ip'] = (string)($outcome['ip'] ?? '');
            } elseif (($outcome['status'] ?? '') == 'failed' && isset($messages[$outcome['reason'] ?? ''])) {
                $account['status'] = 'failed';
                $account['message'] = $messages[$outcome['reason']];
            } else {
                $account['status'] = 'error';
                $account['message'] = $unexpected;
            }
            if ($account['status'] != 'ok') {
                $result['status'] = 'failed';
            }
            $result['accounts'][$uuid] = $account;
        }

        return $result;
    }
}
