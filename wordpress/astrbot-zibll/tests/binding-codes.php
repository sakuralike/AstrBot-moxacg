<?php

namespace AstrBotZibll {
    function random_int($min, $max)
    {
        return !empty($GLOBALS['az_random_codes']) ? array_shift($GLOBALS['az_random_codes']) : \random_int($min, $max);
    }
}

namespace {
    function code_event($name)
    {
        return array('request_id' => hash('sha256', $name), 'event_time' => time());
    }

    function code_actor($name, $group = 'g')
    {
        return array('platform_id' => 'p', 'bot_self_id' => 'bot', 'group_id' => $group, 'sender_id' => 'code-' . $name);
    }

    function code_actor_data($actor)
    {
        $request = new \WP_REST_Request('POST');
        $request->set_header('content-type', 'application/json');
        $request->set_body(wp_json_encode(array('actor' => $actor)));
        return \AstrBotZibll\actor_data($request);
    }

    function start_code_fixture($actor, $event = null)
    {
        clear_fixture_limits();
        $event = $event ?? code_event(bin2hex(random_bytes(16)));
        $response = call_api('binding/code/start', $actor, $event);
        verify($response->get_status() === 200, 'Code session starts');
        $data = $response->get_data()['data'];
        parse_str(parse_url($data['url'], PHP_URL_QUERY), $query);
        $data['token'] = $query['az_bind'];
        return $data;
    }

    function issue_code_fixture($start, $uid, $regenerate = false, $key = null)
    {
        wp_set_current_user($uid);
        return \AstrBotZibll\browser_issue_code($start['token'], array(
            '_wpnonce' => wp_create_nonce('az_code_' . $start['token']),
            'az_action' => 'issue_code', 'regenerate' => $regenerate ? '1' : '0',
            'issue_key' => $key ?? bin2hex(random_bytes(16)),
        ));
    }

    function redeem_code_fixture($actor, $start, $code, $event = null)
    {
        return call_api('binding/code/redeem', $actor, array_merge(
            array('session_id' => $start['session_id'], 'code' => $code), $event ?? code_event(bin2hex(random_bytes(16)))
        ));
    }

    function code_state($response)
    {
        return is_wp_error($response) ? $response->get_error_code()
            : ($response->get_data()['data']['state'] ?? $response->get_data()['code']);
    }

    function parallel_code_workers($payloads)
    {
        global $wpdb;
        $start = microtime(true) + 2;
        $processes = array();
        foreach ($payloads as $payload) {
            $process = proc_open(array(
                PHP_BINARY, __DIR__ . '/business.php', '--code-worker', $wpdb->prefix,
                base64_encode(wp_json_encode($payload)), (string) $start,
            ), array(0 => array('pipe', 'r'), 1 => array('pipe', 'w'), 2 => array('pipe', 'w')), $pipes);
            fclose($pipes[0]);
            $processes[] = array($process, $pipes);
        }
        $results = array();
        foreach ($processes as [$process, $pipes]) {
            $results[] = trim(stream_get_contents($pipes[1]));
            $errors = stream_get_contents($pipes[2]);
            fclose($pipes[1]);
            fclose($pipes[2]);
            verify(proc_close($process) === 0 && $errors === '', 'Concurrent code worker exits cleanly: ' . $errors);
        }
        return $results;
    }

    clear_fixture_limits();
    $cu = wp_insert_user(array('user_login' => 'code-owner', 'user_pass' => wp_generate_password(), 'display_name' => 'Code Owner'));
    $cu2 = wp_insert_user(array('user_login' => 'code-other', 'user_pass' => wp_generate_password(), 'display_name' => 'Private Candidate'));
    $admin = wp_insert_user(array('user_login' => 'code-admin', 'user_pass' => wp_generate_password()));
    (new \WP_User($admin))->add_cap('manage_options');
    $ca = code_actor('owner');
    $start_event = code_event('code-start');
    $cs = start_code_fixture($ca, $start_event);
    $hash = code_actor_data($ca)['actor_hash'];
    verify($cs['session_id'] === hash('sha256', $cs['token']), 'Public token is stored only as a digest');
    verify(code_state(call_api('binding/code/start', $ca, $start_event)) === 'already_started', 'Duplicate start does not replace a session');
    verify(\AstrBotZibll\code_session($cs['session_id'])['state'] === 'pending', 'Original session remains usable');
    verify(code_state(call_api('binding/code/start', $ca, array_merge($start_event, array('event_time' => $start_event['event_time'] - 1)))) === 'request_conflict', 'Start key cannot change input');
    verify(code_state(call_api('binding/code/start', $ca, code_event('bad') + array('uid' => $cu))) === 'invalid_parameters', 'Signed route rejects client UID');
    verify(code_state(call_api('binding/confirm', $ca, array('expected_uid' => $cu))) === 'challenge_expired', 'Old confirm cannot consume a code session');
    verify(code_state(call_api('binding/code/start', $ca, array('request_id' => hash('sha256', 'stale'), 'event_time' => time() - 181))) === 'event_expired', 'Stale binding events rejected');
    wp_set_current_user(0);
    verify(code_state(\AstrBotZibll\browser_issue_code($cs['token'], array())) === 'authentication_failed', 'Anonymous browser cannot issue code');
    wp_set_current_user($cu);
    $valid_input = array(
        '_wpnonce' => wp_create_nonce('az_code_' . $cs['token']), 'az_action' => 'issue_code',
        'issue_key' => bin2hex(random_bytes(16)),
    );
    verify(is_wp_error(\AstrBotZibll\browser_issue_code($cs['token'], array_merge($valid_input, array('uid' => $cu2)))), 'Browser-supplied UID rejected');
    verify(is_wp_error(\AstrBotZibll\browser_issue_code($cs['token'], array_merge($valid_input, array('_wpnonce' => 'invalid')))), 'Browser nonce enforced');
    $GLOBALS['az_random_codes'] = array(1);
    $issued = issue_code_fixture($cs, $cu)->get_data()['data'];
    verify($issued['code'] === '000001' && $issued['expires_in'] <= 300, 'Leading zeros and code lifetime preserved');
    $codes_table = \AstrBotZibll\binding_code_table();
    $sessions_table = \AstrBotZibll\binding_session_table();
    $saved = $wpdb->get_row($wpdb->prepare("SELECT * FROM $codes_table WHERE session_id = %s", $cs['session_id']), ARRAY_A);
    verify($saved['code_hash'] === \AstrBotZibll\code_digest('000001') && $saved['code_hash'] !== hash('sha256', '000001'), 'Code uses keyed digest, not raw SHA');
    verify(!array_key_exists('code', $saved), 'No plaintext code column');
    verify(code_state(issue_code_fixture($cs, $cu)) === 'code_exists', 'Page refresh does not rotate or reveal existing code');
    $GLOBALS['az_random_codes'] = array(1, 2);
    $other_issued = issue_code_fixture($cs, $cu2)->get_data()['data'];
    verify($other_issued['code'] === '000002', 'Collision retries without overwriting another candidate');
    verify((int) $wpdb->get_var($wpdb->prepare("SELECT COUNT(*) FROM $codes_table WHERE session_id = %s AND state = 'active'", $cs['session_id'])) === 2, 'First visitor does not lock out owner');
    $page = render_binding_fixture($cu, $cs['token']);
    verify(str_contains($page, 'data-auto="0"') && !str_contains($page, 'Private Candidate'), 'Page hides other candidates and does not regenerate on refresh');
    verify(!str_contains($page, 'value="000001"'), 'GET never reconstructs plaintext code');
    $manage = render_binding_fixture($cu);
    verify(!str_contains($manage, 'id="az-code-form"'), 'Generic management page cannot issue a transferable code');
    clear_fixture_limits();
    $attacker = code_actor('attacker');
    $as = start_code_fixture($attacker);
    verify(code_state(redeem_code_fixture($attacker, $as, '000001')) === 'code_invalid', 'Different actor cannot steal code');
    verify((int) \AstrBotZibll\code_session($cs['session_id'])['attempts'] === 0, 'Victim attempt budget untouched');
    verify(code_state(redeem_code_fixture(code_actor('owner', 'other'), $cs, '000001')) === 'session_expired', 'Cross-group redemption rejected');
    verify(code_state(redeem_code_fixture($attacker, $cs, '000001')) === 'session_expired', 'Actor cannot choose another session');
    $event = code_event('owner-redeem');
    $ok = redeem_code_fixture($ca, $cs, '000001', $event);
    verify(code_state($ok) === 'bound' && $ok->get_data()['data']['uid'] === $cu, 'Original actor binds chosen logged-in account');
    verify(code_state(redeem_code_fixture($ca, $cs, '000001', $event)) === 'bound', 'Identical delivery is idempotent');
    verify(code_state(redeem_code_fixture($ca, $cs, '000002', $event)) === 'request_conflict', 'Same event cannot change code');
    verify((int) $wpdb->get_var($wpdb->prepare("SELECT COUNT(*) FROM $codes_table WHERE session_id = %s AND state = 'active'", $cs['session_id'])) === 0, 'Every remaining candidate invalidated after binding');
    clear_fixture_limits();
    verify(code_state(call_api('binding/code/start', $ca, code_event('already-bound'))) === 'bound', 'Already-bound start does not replace UID');
    \AstrBotZibll\install();
    verify((int) \AstrBotZibll\binding_row($hash)['user_id'] === $cu, 'Repeated schema upgrade preserves existing bindings');
    verify(code_state(\AstrBotZibll\revoke_binding($hash, $cu2)) === 'binding_changed', 'Website cannot revoke another account');
    verify(code_state(\AstrBotZibll\revoke_binding($hash, $cu)) === 'revoked', 'Website owner can revoke');
    verify(code_state(redeem_code_fixture($ca, $cs, '000001', $event)) === 'revoked', 'Old success after revocation never restores binding');
    verify(code_state(call_api('binding/code/status', $ca, array('session_id' => $cs['session_id'], 'request_id' => $event['request_id']))) === 'revoked', 'Status checks current authorization, not historical success');

    $limit_actor = code_actor('limit');
    $ls = start_code_fixture($limit_actor);
    $li = issue_code_fixture($ls, $cu)->get_data()['data'];
    $wrong = $li['code'] === '999999' ? '999998' : '999999';
    $wrong_event = code_event('wrong-once');
    verify(code_state(redeem_code_fixture($limit_actor, $ls, $wrong, $wrong_event)) === 'code_invalid', 'Wrong code is rejected');
    verify(code_state(redeem_code_fixture($limit_actor, $ls, $wrong, $wrong_event)) === 'code_invalid', 'Duplicate wrong message returns same result');
    verify((int) \AstrBotZibll\code_session($ls['session_id'])['attempts'] === 1, 'Duplicate wrong message does not spend attempts again');
    $regen_key = bin2hex(random_bytes(16));
    $replacement = issue_code_fixture($ls, $cu, true, $regen_key)->get_data()['data'];
    verify(code_state(issue_code_fixture($ls, $cu, true, $regen_key)) === 'code_exists', 'Repeated POST cannot reissue the code');
    verify((int) \AstrBotZibll\code_session($ls['session_id'])['attempts'] === 1, 'Regeneration does not reset failures');
    verify(code_state(redeem_code_fixture($limit_actor, $ls, $li['code'])) === 'code_invalid', 'Regeneration invalidates old code');
    for ($i = 0; $i < 3; ++$i) {
        $result = redeem_code_fixture($limit_actor, $ls, $wrong);
    }
    verify(code_state($result) === 'too_many_attempts', 'Five errors lock parent session');
    verify(code_state(issue_code_fixture($ls, $cu, true)) === 'too_many_attempts', 'Regeneration cannot unlock parent');
    clear_fixture_limits();
    verify(code_state(redeem_code_fixture($limit_actor, $ls, $replacement['code'])) === 'too_many_attempts', 'Correct code cannot bypass locked parent');

    $expire_actor = code_actor('expiry');
    $es = start_code_fixture($expire_actor);
    $ei = issue_code_fixture($es, $cu)->get_data()['data'];
    $wpdb->update($codes_table, array('expires_at' => time() - 1), array('session_id' => $es['session_id']));
    verify(code_state(redeem_code_fixture($expire_actor, $es, $ei['code'])) === 'code_invalid', 'Expired code cannot bind');
    $reissue = issue_code_fixture($es, $cu, true)->get_data()['data'];
    $wpdb->update($sessions_table, array('expires_at' => time() - 1), array('session_id' => $es['session_id']));
    verify(code_state(redeem_code_fixture($expire_actor, $es, $reissue['code'])) === 'session_expired', 'Parent expiry is authoritative');
    $es2 = start_code_fixture($expire_actor);
    verify(code_state(redeem_code_fixture($expire_actor, $es, $reissue['code'])) === 'session_expired', 'Restart invalidates prior session');
    verify(code_state(call_api('binding/confirm', $expire_actor, array('expected_uid' => $cu))) === 'challenge_expired', 'New session remains isolated from legacy confirm');

    $fault_actor = code_actor('fault');
    $fs = start_code_fixture($fault_actor);
    $fi = issue_code_fixture($fs, $cu)->get_data()['data'];
    $fault = static function ($query) use ($sessions_table) {
        return str_starts_with($query, "UPDATE `$sessions_table`") && str_contains($query, "'bound'")
            ? 'AZ_INJECTED_DATABASE_FAILURE' : $query;
    };
    add_filter('query', $fault);
    $failed_event = code_event('fault-redeem');
    $result = redeem_code_fixture($fault_actor, $fs, $fi['code'], $failed_event);
    remove_filter('query', $fault);
    verify(code_state($result) === 'unavailable', 'Mid-transaction failure is not reported as success');
    verify(!\AstrBotZibll\binding_row(code_actor_data($fault_actor)['actor_hash'])['user_id'], 'Binding write rolls back with session failure');
    verify(\AstrBotZibll\code_session($fs['session_id'])['state'] === 'pending', 'Failed transaction leaves session retryable');
    verify(code_state(call_api('binding/code/status', $fault_actor, array('session_id' => $fs['session_id'], 'request_id' => $failed_event['request_id']))) === 'not_processed', 'Uncommitted event status remains unknown');
    $event = code_event('parallel-same-code');
    $payload = array('action' => 'redeem', 'actor' => code_actor_data($fault_actor), 'input' => array_merge(
        array('session_id' => $fs['session_id'], 'code' => $fi['code']), $event
    ));
    $states = parallel_code_workers(array_fill(0, 4, $payload));
    verify(count(array_filter($states, static fn($state) => $state === 'bound')) === 4, 'Concurrent repeated message returns one idempotent result');
    verify((int) \AstrBotZibll\binding_row($payload['actor']['actor_hash'])['user_id'] === $cu, 'Concurrent code writes one binding');
    verify(code_state(call_api('binding/code/status', $fault_actor, array('session_id' => $fs['session_id'], 'request_id' => $event['request_id']))) === 'bound', 'Lost committed response can be recovered by status');

    $same_uid_actors = array(code_actor('unique-a'), code_actor('unique-b'));
    $unique_payloads = array();
    foreach ($same_uid_actors as $a) {
        $s = start_code_fixture($a);
        $c = issue_code_fixture($s, $cu2)->get_data()['data'];
        $unique_payloads[] = array('action' => 'redeem', 'actor' => code_actor_data($a), 'input' => array_merge(
            array('session_id' => $s['session_id'], 'code' => $c['code']), code_event($a['sender_id'])
        ));
    }
    $states = parallel_code_workers($unique_payloads);
    sort($states);
    verify($states === array('binding_conflict', 'bound'), 'Scope UID unique key arbitrates competing actors: ' . wp_json_encode($states));

    $admin_hash = $payload['actor']['actor_hash'];
    wp_set_current_user($cu);
    $admin_input = array('az_action' => 'revoke_one', 'binding' => $admin_hash, '_wpnonce' => 'bad');
    verify(code_state(\AstrBotZibll\admin_revoke_bindings($admin_input)) === 'forbidden', 'Ordinary website user cannot revoke via admin');
    wp_set_current_user($admin);
    verify(code_state(\AstrBotZibll\admin_revoke_bindings($admin_input)) === 'invalid_nonce', 'Admin needs a valid nonce');
    $admin_input['_wpnonce'] = wp_create_nonce('astrbot_zibll_bindings');
    $before_posts = (int) $wpdb->get_var("SELECT COUNT(*) FROM {$wpdb->posts}");
    $before_comments = (int) $wpdb->get_var("SELECT COUNT(*) FROM {$wpdb->comments}");
    $before_points = zibpay_get_user_points($cu);
    $states = parallel_code_workers(array($payload, array('action' => 'admin', 'hash' => $admin_hash, 'uid' => $admin)));
    verify(in_array('revoked', $states, true), 'Concurrent admin revoke completes');
    verify(!\AstrBotZibll\binding_row($admin_hash)['user_id'], 'Admin revoke cannot be undone by old code');
    verify(\AstrBotZibll\admin_revoke_bindings($admin_input)[0]['state'] === 'already_revoked', 'Admin repeated revoke is idempotent');
    $selected = $unique_payloads[0]['actor']['actor_hash'];
    $untouched = $unique_payloads[1]['actor']['actor_hash'];
    $before_untouched = \AstrBotZibll\binding_row($untouched);
    $batch = \AstrBotZibll\admin_revoke_bindings(array(
        'az_action' => 'revoke_batch', '_wpnonce' => wp_create_nonce('astrbot_zibll_bindings'),
        'bindings' => array($selected, $admin_hash, str_repeat('f', 64), 'invalid'),
    ));
    verify(count($batch) === 4 && $batch[0]['state'] === 'revoked' && $batch[1]['state'] === 'already_revoked'
        && $batch[2]['state'] === 'not_found' && $batch[3]['state'] === 'invalid_parameters', 'Batch reports valid, revoked, missing, malformed independently');
    verify(\AstrBotZibll\binding_row($untouched) === $before_untouched, 'Batch does not touch unselected binding');
    verify($before_posts === (int) $wpdb->get_var("SELECT COUNT(*) FROM {$wpdb->posts}")
        && $before_comments === (int) $wpdb->get_var("SELECT COUNT(*) FROM {$wpdb->comments}")
        && $before_points === zibpay_get_user_points($cu) && get_userdata($cu), 'Revocation preserves site users and assets');
    clear_fixture_limits();
    verify(code_state(call_api('profile', $fault_actor)) === 'not_bound', 'Revoked profile access rejected');
    verify(code_state(call_api('checkin', $fault_actor, code_event('after-revoke'))) === 'not_bound', 'Revoked checkin rejected');
    verify(call_api('articles/latest', $fault_actor)->get_status() === 200, 'Public article policy is not changed by revocation');
    $audits = $wpdb->get_col('SELECT payload FROM ' . \AstrBotZibll\state_table() . " WHERE payload LIKE '%administrator%'");
    verify(count($audits) >= 4 && json_decode($audits[0], true)['administrator'] === $admin, 'Admin audit stores operator and per-record outcome');
    ob_start();
    \AstrBotZibll\render_admin_bindings($batch);
    $admin_html = ob_get_clean();
    verify(str_contains($admin_html, '撤销勾选授权') && str_contains($admin_html, 'az-one-'), 'Admin exposes batch and isolated single forms');
    verify(!str_contains($admin_html, 'id="az-code"') && !str_contains($admin_html, 'code_hash'), 'Admin UI does not expose authorization codes');
    $saved_get = $_GET;
    $_GET = array('az_filter' => 'code-fault', 'az_status' => 'revoked', 'az_page' => 1);
    ob_start();
    \AstrBotZibll\render_admin_bindings(null);
    $filtered_admin_html = ob_get_clean();
    $_GET = $saved_get;
    verify(str_contains($filtered_admin_html, 'id="az-binding-filter"')
        && str_contains($filtered_admin_html, 'name="az_status"')
        && str_contains($filtered_admin_html, 'value="' . esc_attr($admin_hash) . '"'), 'Admin binding filters preserve matching records');

    $page_actor = code_actor('page');
    $ps = start_code_fixture($page_actor);
    $page = render_binding_fixture($cu, $ps['token']);
    $pending_page = $page;
    verify(str_contains($page, 'data-auto="1"') && str_contains($page, 'requestSubmit'), 'First login page auto-POSTs once');
    $issued_page = render_binding_fixture($cu, $ps['token'], 'issue_code');
    verify(preg_match('/id="az-code"[^>]*value="[0-9]{6}"/', $issued_page) === 1, 'Browser POST displays six-digit code');
    verify(str_contains($issued_page, 'data-auto="0"') && str_contains($issued_page, '重新生成'), 'Issued page cannot auto-loop');
    verify(!\AstrBotZibll\binding_row(code_actor_data($page_actor)['actor_hash'])['user_id'], 'Auto issuing still requires group redemption');
    $second_page = render_binding_fixture($cu2, $ps['token']);
    verify(str_contains($second_page, 'data-auto="1"'), 'Another logged-in user has independent candidate state');
    $scope_options = \AstrBotZibll\settings();
    update_option(\AstrBotZibll\OPTION, array_merge($scope_options, array('allowed_groups' => array('p|bot|other'))), false);
    verify(code_state(issue_code_fixture($ps, $cu, true)) === 'session_expired', 'Removed group cannot issue existing session codes');
    verify(redeem_code_fixture($page_actor, $ps, '000000')->get_status() === 403, 'Removed group cannot redeem');
    update_option(\AstrBotZibll\OPTION, $scope_options, false);
    $paused = \AstrBotZibll\settings();
    update_option(\AstrBotZibll\OPTION, array_merge($paused, array('enabled' => false)), false);
    verify(code_state(issue_code_fixture($ps, $cu, true)) === 'unavailable', 'Disabled service stops issue');
    update_option(\AstrBotZibll\OPTION, $paused, false);
    update_user_meta($cu, 'banned', 2);
    verify(code_state(issue_code_fixture($ps, $cu, true)) === 'account_restricted', 'Banned account cannot issue');
    preg_match('/id="az-code"[^>]*value="([0-9]{6})"/', $issued_page, $match);
    clear_fixture_limits();
    verify(code_state(redeem_code_fixture($page_actor, $ps, $match[1])) === 'account_restricted', 'Banned account cannot redeem an already-issued code');
    delete_user_meta($cu, 'banned');

    $quota_actor = code_actor('quotas');
    $qs = start_code_fixture($quota_actor);
    $quota_uids = array();
    for ($i = 0; $i < 6; ++$i) {
        $quota_uids[] = wp_insert_user(array('user_login' => 'quota-' . $i, 'user_pass' => wp_generate_password()));
        $result = issue_code_fixture($qs, $quota_uids[$i]);
        verify(code_state($result) === ($i < 5 ? 'issued' : 'rate_limited'), 'Five simultaneous candidates is a strict ceiling');
    }
    verify((int) $wpdb->get_var($wpdb->prepare(
        "SELECT COUNT(*) FROM $codes_table WHERE session_id = %s AND state = 'active'", $qs['session_id']
    )) === 5, 'Candidate overflow does not evict others');
    $expiry_before = \AstrBotZibll\code_session($qs['session_id'])['expires_at'];
    $key = bin2hex(random_bytes(16));
    verify(code_state(issue_code_fixture($qs, $quota_uids[0], true, $key)) === 'issued', 'Candidate can regenerate under cap');
    verify(code_state(issue_code_fixture($qs, $quota_uids[0], true, $key)) === 'code_exists', 'Duplicate regenerate request stays idempotent');
    verify(code_state(issue_code_fixture($qs, $quota_uids[0], true)) === 'issued', 'Third issue in fixed window allowed');
    verify(code_state(issue_code_fixture($qs, $quota_uids[0], true)) === 'rate_limited', 'Fourth issue in fixed window blocked');
    verify(\AstrBotZibll\code_session($qs['session_id'])['expires_at'] === $expiry_before, 'Regeneration never extends parent TTL');
    clear_fixture_limits();
    for ($i = 0; $i < 10; ++$i) {
        \AstrBotZibll\rate_limit('issue-user:' . $quota_uids[1], 10, 300);
    }
    verify(code_state(issue_code_fixture($qs, $quota_uids[1], true)) === 'rate_limited', 'UID quota applies across candidate sessions');
    clear_fixture_limits();
    $start_quota_actor = code_actor('start-quota');
    for ($i = 0; $i < 4; ++$i) {
        $state = code_state(call_api('binding/code/start', $start_quota_actor, code_event('start-quota-' . $i)));
        verify($state === ($i < 3 ? 'pending' : 'rate_limited'), 'Start quota cannot be bypassed by new message IDs');
    }

    clear_fixture_limits();
    $pending_admin_actor = code_actor('pending-admin');
    $pas = start_code_fixture($pending_admin_actor);
    $pac = issue_code_fixture($pas, $quota_uids[5])->get_data()['data'];
    $pending_hash = code_actor_data($pending_admin_actor)['actor_hash'];
    $pending_payload = array('action' => 'redeem', 'actor' => code_actor_data($pending_admin_actor), 'input' => array_merge(
        array('session_id' => $pas['session_id'], 'code' => $pac['code']), code_event('pending-admin-race')
    ));
    $states = parallel_code_workers(array($pending_payload, array('action' => 'admin', 'hash' => $pending_hash, 'uid' => $admin)));
    verify(in_array('revoked', $states, true), 'Admin can cancel pending session or just-completed binding');
    verify(!\AstrBotZibll\binding_row($pending_hash)['user_id'], 'Pending admin revoke/redemption race ends revoked');
    verify(!in_array(code_state(redeem_code_fixture($pending_admin_actor, $pas, $pac['code'])), array('bound', 'code_invalid'), true), 'Cancelled pending code cannot revive');

    $web_actor = code_actor('website-race');
    $ws = start_code_fixture($web_actor);
    $wc = issue_code_fixture($ws, $quota_uids[5])->get_data()['data'];
    $web_event = code_event('website-race-bound');
    verify(code_state(redeem_code_fixture($web_actor, $ws, $wc['code'], $web_event)) === 'bound', 'Website race fixture binds');
    $whash = code_actor_data($web_actor)['actor_hash'];
    $states = parallel_code_workers(array(
        array('action' => 'redeem', 'actor' => code_actor_data($web_actor), 'input' => array_merge(
            array('session_id' => $ws['session_id'], 'code' => $wc['code']), $web_event
        )),
        array('action' => 'revoke', 'hash' => $whash, 'uid' => $quota_uids[5]),
    ));
    verify(in_array('revoked', $states, true) && !\AstrBotZibll\binding_row($whash)['user_id'], 'Website revoke wins against old successful message without recreating authorization');

    $del_actor = code_actor('deleted');
    $ds = start_code_fixture($del_actor);
    $dc = issue_code_fixture($ds, $quota_uids[5])->get_data()['data'];
    require_once ABSPATH . 'wp-admin/includes/user.php';
    wp_delete_user($quota_uids[5]);
    clear_fixture_limits();
    verify(code_state(redeem_code_fixture($del_actor, $ds, $dc['code'])) !== 'bound', 'Deleted user code cannot bind');
    $deleted_codes = (int) $wpdb->get_var($wpdb->prepare(
        "SELECT COUNT(*) FROM $codes_table WHERE user_id = %d AND state = 'active'", $quota_uids[5]
    ));
    verify($deleted_codes === 0, 'Deleting website user invalidates only its candidate codes');

    $original_filter = static fn() => 'test-rotated-auth-salt';
    add_filter('salt', $original_filter);
    verify(\AstrBotZibll\code_digest('000001') !== $saved['code_hash'], 'Salt rotation invalidates old digest lookup');
    remove_filter('salt', $original_filter);
    $before_engine = \AstrBotZibll\binding_transactions_ready();
    $wpdb->query("ALTER TABLE $codes_table ENGINE=MyISAM");
    verify($before_engine && !\AstrBotZibll\binding_transactions_ready(), 'Nontransactional schema is rejected');
    clear_fixture_limits();
    verify(code_state(call_api('binding/code/start', code_actor('bad-engine'), code_event('bad-engine'))) === 'unavailable', 'Code flow fails closed on nontransactional tables');
    $wpdb->query("ALTER TABLE $codes_table ENGINE=InnoDB");
    verify(\AstrBotZibll\binding_transactions_ready(), 'Only isolated fixture engine is restored');
    wp_set_current_user($admin);
    if ($preview_dir = getenv('AZ_BINDING_PREVIEW_DIR')) {
        foreach (array(
            'code-pending' => $pending_page, 'code-issued' => $issued_page, 'code-other-account' => $second_page,
            'code-management' => $manage, 'code-admin' => $admin_html,
        ) as $name => $html) {
            file_put_contents($preview_dir . '/' . $name . '.html', $html);
        }
    }
}
