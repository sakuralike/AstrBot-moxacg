<?php

namespace AstrBotZibll;

defined('ABSPATH') || exit;

const POINTS_MAX_AMOUNT = 100000;

function points_table()
{
    global $wpdb;
    return $wpdb->prefix . 'astrbot_zibll_points_operations';
}

function install_points($collate)
{
    $table = points_table();
    dbDelta("CREATE TABLE $table (
        request_key char(64) NOT NULL,
        request_id char(64) NOT NULL,
        input_hash char(64) NOT NULL,
        action varchar(16) NOT NULL,
        actor_hash char(64) NOT NULL,
        target_actor_hash char(64) NOT NULL,
        user_id bigint(20) unsigned DEFAULT NULL,
        amount int(10) unsigned NOT NULL,
        points_before bigint(20) NOT NULL DEFAULT 0,
        points_after bigint(20) NOT NULL DEFAULT 0,
        state varchar(24) NOT NULL,
        error_code varchar(64) NOT NULL DEFAULT '',
        created_at bigint(20) unsigned NOT NULL,
        updated_at bigint(20) unsigned NOT NULL,
        expires_at bigint(20) unsigned NOT NULL,
        PRIMARY KEY (request_key),
        KEY request_id (request_id),
        KEY actor_hash (actor_hash),
        KEY target_actor_hash (target_actor_hash),
        KEY user_id (user_id),
        KEY expires_at (expires_at)
    ) ENGINE=InnoDB $collate;");
}

function points_schema_ready()
{
    global $wpdb;
    $table = points_table();
    foreach (array(
        'request_id', 'input_hash', 'action', 'actor_hash', 'target_actor_hash',
        'user_id', 'amount', 'points_before', 'points_after', 'state',
        'error_code', 'created_at', 'updated_at', 'expires_at',
    ) as $column) {
        if (!$wpdb->get_var($wpdb->prepare("SHOW COLUMNS FROM $table LIKE %s", $column))) {
            return false;
        }
    }
    return true;
}

function points_transactions_ready()
{
    global $wpdb;
    $tables = array($wpdb->usermeta, points_table());
    foreach ($tables as $table) {
        $engine = $wpdb->get_var($wpdb->prepare(
            'SELECT engine FROM information_schema.tables
             WHERE table_schema = DATABASE() AND table_name = %s',
            $table
        ));
        if (strtoupper((string) $engine) !== 'INNODB') {
            return false;
        }
    }
    return true;
}

function points_request_key($actor_hash, $request_id)
{
    return hash('sha256', 'points:' . $actor_hash . ':' . $request_id);
}

function points_input_hash($action, $actor, $target, $amount, $event_time)
{
    return hash('sha256', wp_json_encode(array(
        'action' => $action,
        'actor' => $actor['actor_hash'],
        'target' => $target['actor_hash'],
        'amount' => $amount,
        'event_time' => $event_time,
    ), JSON_UNESCAPED_UNICODE));
}

function points_response($row)
{
    $user = $row['user_id'] !== null ? get_userdata((int) $row['user_id']) : false;
    $data = array(
        'state' => $row['state'],
        'request_id' => $row['request_id'],
        'action' => $row['action'],
        'uid' => $row['user_id'] !== null ? (int) $row['user_id'] : null,
        'nickname' => $user ? (string) $user->display_name : '',
        'amount' => (int) $row['amount'],
        'points_before' => (int) $row['points_before'],
        'points_after' => (int) $row['points_after'],
    );
    if ($row['error_code'] !== '') {
        $data['error_code'] = $row['error_code'];
    }
    return success($data);
}

function points_validate_amount($amount)
{
    if (!is_int($amount) || $amount < 1 || $amount > POINTS_MAX_AMOUNT) {
        return failure('invalid_parameters', 400);
    }
    return true;
}

function points_validate_event($input)
{
    $request_id = $input['request_id'] ?? null;
    $event_time = $input['event_time'] ?? null;
    if (!is_string($request_id) || !preg_match('/\A[0-9a-f]{64}\z/', $request_id)) {
        return failure('event_identity_required', 400);
    }
    if (!is_int($event_time)) {
        return failure('event_identity_required', 400);
    }
    if (abs(time() - $event_time) > CLOCK_WINDOW) {
        return failure('event_expired', 409);
    }
    return array('request_id' => $request_id, 'event_time' => $event_time);
}

function points_target($actor, $target)
{
    $scope = implode('|', array(
        json_decode($actor['identity'], true)['platform_id'],
        json_decode($actor['identity'], true)['bot_self_id'],
        json_decode($actor['identity'], true)['group_id'],
    ));
    return actor_data_from_array($target, $scope);
}

function points_operation_row($request_key)
{
    global $wpdb;
    return $wpdb->get_row($wpdb->prepare(
        'SELECT * FROM ' . points_table() . ' WHERE request_key = %s', $request_key
    ), ARRAY_A);
}

function points_lock_user_meta($uid)
{
    global $wpdb;
    $wpdb->get_results($wpdb->prepare(
        "SELECT umeta_id FROM {$wpdb->usermeta}
         WHERE user_id = %d AND meta_key IN ('points', 'zib_other_data') FOR UPDATE",
        $uid
    ));
    return $wpdb->last_error === '';
}

function points_record_matches($uid, $order_num, $delta, $after)
{
    $records = zib_get_user_meta($uid, 'points_record', true);
    if (!is_array($records)) {
        return false;
    }
    $matches = 0;
    foreach ($records as $record) {
        if (is_array($record) && ($record['order_num'] ?? null) === $order_num) {
            if ((int) ($record['value'] ?? 0) !== $delta
                || (int) ($record['points'] ?? -1) !== $after
            ) {
                return false;
            }
            ++$matches;
        }
    }
    return $matches === 1;
}

function points_has_order_num($uid, $order_num)
{
    foreach ((array) zib_get_user_meta($uid, 'points_record', true) as $record) {
        if (is_array($record) && ($record['order_num'] ?? null) === $order_num) {
            return true;
        }
    }
    return false;
}

function points_finish($request_key, $state, $error_code, $before, $after)
{
    global $wpdb;
    if ($wpdb->update(points_table(), array(
        'points_before' => $before,
        'points_after' => $after,
        'state' => $state,
        'error_code' => $error_code,
        'updated_at' => time(),
    ), array('request_key' => $request_key)) === false) {
        return failure('unavailable', 503);
    }
    $row = points_operation_row($request_key);
    return $row ? points_response($row) : failure('unavailable', 503);
}

function points_cleanup()
{
    global $wpdb;
    $wpdb->query($wpdb->prepare(
        'DELETE FROM ' . points_table() . ' WHERE expires_at < %d LIMIT 100', time()
    ));
}

function points_action($action, $actor, $input)
{
    if (!settings()['enabled']) {
        return failure('forbidden', 403);
    }
    if (!in_array($action, array('points/add', 'points/subtract', 'points/status'), true)) {
        return failure('invalid_parameters', 400);
    }
    if ($action === 'points/status') {
        if (!points_schema_ready()) {
            return failure('unavailable', 503);
        }
        return points_status($actor, $input);
    }
    if (!points_schema_ready()) {
        return failure('unavailable', 503);
    }
    if (!points_transactions_ready()) {
        return failure('unavailable', 503);
    }
    if (!_pz('points_s')) {
        return failure('points_disabled', 403);
    }
    $amount = $input['amount'] ?? null;
    $valid_amount = points_validate_amount($amount);
    if (is_wp_error($valid_amount)) {
        return $valid_amount;
    }
    $event = points_validate_event($input);
    if (is_wp_error($event)) {
        return $event;
    }
    $target = points_target($actor, $input['target_actor'] ?? null);
    if (is_wp_error($target)) {
        return $target;
    }
    $request_key = points_request_key($actor['actor_hash'], $event['request_id']);
    $input_hash = points_input_hash($action, $actor, $target, $amount, $event['event_time']);
    points_cleanup();
    return with_lock('points:actor:' . $actor['actor_hash'], static function () use (
        $action, $actor, $target, $amount, $event, $request_key, $input_hash
    ) {
        global $wpdb;
        $existing = points_operation_row($request_key);
        if ($existing) {
            return hash_equals($existing['input_hash'], $input_hash)
                ? points_response($existing) : failure('request_conflict', 409);
        }
        return with_lock('actor:' . $target['actor_hash'], static function () use (
            $action, $actor, $target, $amount, $event, $request_key, $input_hash, $wpdb
        ) {
            $existing = points_operation_row($request_key);
            if ($existing) {
                return hash_equals($existing['input_hash'], $input_hash)
                    ? points_response($existing) : failure('request_conflict', 409);
            }
            $user = bound_user($target['actor_hash']);
            if (is_wp_error($user)) {
                return $user->get_error_code() === 'not_bound'
                    ? failure('target_not_bound', 404) : $user;
            }
            return with_lock('checkin:' . (int) $user->ID, static function () use (
                $action, $actor, $target, $amount, $event, $request_key, $input_hash, $wpdb, $user
            ) {
                $existing = points_operation_row($request_key);
                if ($existing) {
                    return hash_equals($existing['input_hash'], $input_hash)
                        ? points_response($existing) : failure('request_conflict', 409);
                }
                if (!points_transactions_ready()) {
                    return failure('unavailable', 503);
                }
                fresh_user_meta((int) $user->ID);
                $now = time();
                $saved = $wpdb->insert(points_table(), array(
                    'request_key' => $request_key,
                    'request_id' => $event['request_id'],
                    'input_hash' => $input_hash,
                    'action' => $action === 'points/add' ? 'add' : 'subtract',
                    'actor_hash' => $actor['actor_hash'],
                    'target_actor_hash' => $target['actor_hash'],
                    'user_id' => (int) $user->ID,
                    'amount' => $amount,
                    'points_before' => 0,
                    'points_after' => 0,
                    'state' => 'pending',
                    'error_code' => '',
                    'created_at' => $now,
                    'updated_at' => $now,
                    'expires_at' => $now + 7 * DAY_IN_SECONDS,
                ));
                if ($saved !== 1) {
                    $existing = points_operation_row($request_key);
                    return $existing && hash_equals($existing['input_hash'], $input_hash)
                        ? points_response($existing) : failure('unavailable', 503);
                }
                $delta = $action === 'points/add' ? $amount : -$amount;
                $uid = (int) $user->ID;
                $order_num = 'astrbot:' . substr($request_key, 0, 24);
                if ($wpdb->query('START TRANSACTION') === false) {
                    return points_finish($request_key, 'uncertain', 'points_transaction_failed', 0, 0);
                }
                if (!points_lock_user_meta($uid)) {
                    $wpdb->query('ROLLBACK');
                    fresh_user_meta($uid);
                    return points_finish($request_key, 'uncertain', 'points_transaction_failed', 0, 0);
                }
                fresh_user_meta($uid);
                $before = (int) zibpay_get_user_points($uid);
                $after = $before;
                if ($delta < 0 && $before < abs($delta)) {
                    $wpdb->query('ROLLBACK');
                    $finished = points_finish($request_key, 'rejected', 'insufficient_points', $before, $before);
                    return is_wp_error($finished) ? $finished : failure('insufficient_points', 409);
                }
                if (points_has_order_num($uid, $order_num)) {
                    $wpdb->query('ROLLBACK');
                    return points_finish($request_key, 'uncertain', 'points_record_conflict', $before, $before);
                }
                if ($wpdb->update(points_table(), array(
                    'points_before' => $before, 'points_after' => $before, 'updated_at' => time(),
                ), array('request_key' => $request_key)) === false) {
                    $wpdb->query('ROLLBACK');
                    fresh_user_meta($uid);
                    return points_finish($request_key, 'uncertain', 'points_transaction_failed', 0, 0);
                }
                try {
                    $result = zibpay_update_user_points($uid, array(
                        'value' => $delta,
                        'type' => $action === 'points/add' ? 'AstrBot 管理员加分' : 'AstrBot 管理员减分',
                        'desc' => 'AstrBot 群聊管理员积分调整',
                        'order_num' => $order_num,
                    ));
                } catch (\Throwable $error) {
                    $result = false;
                }
                fresh_user_meta($uid);
                $after = (int) zibpay_get_user_points($uid);
                $expected = $before + $delta;
                $complete = $result === true && $wpdb->last_error === ''
                    && $after === $expected
                    && points_record_matches($uid, $order_num, $delta, $expected);
                if ($complete) {
                    $finished = points_finish($request_key, 'completed', '', $before, $after);
                    if (is_wp_error($finished) || $wpdb->query('COMMIT') === false) {
                        $wpdb->query('ROLLBACK');
                        fresh_user_meta($uid);
                        return failure('uncertain', 503);
                    }
                    fresh_user_meta($uid);
                    return $finished;
                }
                $wpdb->query('ROLLBACK');
                fresh_user_meta($uid);
                $observed_after = (int) zibpay_get_user_points($uid);
                $rolled_back = $observed_after === $before
                    && !points_has_order_num($uid, $order_num);
                $state = $rolled_back ? 'rejected' : 'uncertain';
                $error_code = $rolled_back ? 'points_update_failed' : 'points_update_uncertain';
                $finished = points_finish($request_key, $state, $error_code, $before, $observed_after);
                if (is_wp_error($finished)) {
                    return $finished;
                }
                return $state === 'rejected' ? failure($error_code, 503) : failure('uncertain', 503);
            });
        });
    });
}

function points_status($actor, $input)
{
    $request_id = $input['request_id'] ?? null;
    if (!is_string($request_id) || !preg_match('/\A[0-9a-f]{64}\z/', $request_id)) {
        return failure('event_identity_required', 400);
    }
    $row = points_operation_row(points_request_key($actor['actor_hash'], $request_id));
    if (!$row) {
        return success(array('state' => 'not_started', 'request_id' => $request_id));
    }
    return points_response($row);
}
