<?php

namespace AstrBotZibll;

defined('ABSPATH') || exit;

const BLINDBOX_MAX_PRIZES = 50;
const BLINDBOX_MAX_LIMIT = 1000;
const BLINDBOX_MAX_POINTS = 100000;

function blindbox_default_config()
{
    return array(
        'enabled' => false,
        'daily_free_limit' => 5,
        'daily_exchange_limit' => 5,
        'exchange_cost_points' => 25,
        'prizes' => array(array(
            'id' => 'points_default',
            'name' => '积分盲盒',
            'type' => 'points',
            'probability' => '100.00',
            'probability_bp' => 10000,
            'points_min' => 50,
            'points_max' => 300,
        )),
    );
}

function blindbox_daily_table()
{
    global $wpdb;
    return $wpdb->prefix . 'astrbot_zibll_blindbox_daily';
}

function blindbox_operation_table()
{
    global $wpdb;
    return $wpdb->prefix . 'astrbot_zibll_blindbox_operations';
}

function install_blindbox($collate)
{
    $daily = blindbox_daily_table();
    $operations = blindbox_operation_table();
    dbDelta("CREATE TABLE $daily (
        user_id bigint(20) unsigned NOT NULL,
        site_day date NOT NULL,
        free_used int(10) unsigned NOT NULL DEFAULT 0,
        exchange_bought int(10) unsigned NOT NULL DEFAULT 0,
        exchange_used int(10) unsigned NOT NULL DEFAULT 0,
        updated_at bigint(20) unsigned NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id,site_day),
        KEY site_day (site_day)
    ) ENGINE=InnoDB $collate;");
    dbDelta("CREATE TABLE $operations (
        request_key char(64) NOT NULL,
        request_id char(64) NOT NULL,
        input_hash char(64) NOT NULL,
        actor_hash char(64) NOT NULL,
        user_id bigint(20) unsigned NOT NULL,
        site_day date NOT NULL,
        timezone varchar(64) NOT NULL,
        action varchar(24) NOT NULL,
        rule_hash char(64) NOT NULL,
        rule_json longtext NOT NULL,
        prize_json longtext DEFAULT NULL,
        result_json longtext DEFAULT NULL,
        use_source varchar(16) NOT NULL DEFAULT '',
        points_before bigint(20) NOT NULL DEFAULT 0,
        points_delta bigint(20) NOT NULL DEFAULT 0,
        points_after bigint(20) NOT NULL DEFAULT 0,
        free_used_before int(10) unsigned NOT NULL DEFAULT 0,
        free_used_after int(10) unsigned NOT NULL DEFAULT 0,
        exchange_bought_before int(10) unsigned NOT NULL DEFAULT 0,
        exchange_bought_after int(10) unsigned NOT NULL DEFAULT 0,
        exchange_used_before int(10) unsigned NOT NULL DEFAULT 0,
        exchange_used_after int(10) unsigned NOT NULL DEFAULT 0,
        state varchar(24) NOT NULL,
        error_code varchar(64) NOT NULL DEFAULT '',
        created_at bigint(20) unsigned NOT NULL,
        updated_at bigint(20) unsigned NOT NULL,
        expires_at bigint(20) unsigned NOT NULL,
        PRIMARY KEY (request_key),
        KEY request_id (request_id),
        KEY actor_hash (actor_hash),
        KEY user_day (user_id,site_day),
        KEY state (state),
        KEY expires_at (expires_at)
    ) ENGINE=InnoDB $collate;");
}

function blindbox_schema_ready()
{
    global $wpdb;
    foreach (array(
        blindbox_daily_table() => array(
            'free_used', 'exchange_bought', 'exchange_used', 'updated_at',
        ),
        blindbox_operation_table() => array(
            'request_id', 'input_hash', 'actor_hash', 'user_id', 'site_day',
            'timezone', 'action', 'rule_hash', 'rule_json', 'prize_json',
            'result_json', 'use_source', 'points_before', 'points_delta',
            'points_after', 'free_used_before', 'free_used_after',
            'exchange_bought_before', 'exchange_bought_after',
            'exchange_used_before', 'exchange_used_after', 'state',
            'error_code', 'created_at', 'updated_at', 'expires_at',
        ),
    ) as $table => $columns) {
        foreach ($columns as $column) {
            if (!$wpdb->get_var($wpdb->prepare(
                "SHOW COLUMNS FROM $table LIKE %s", $column
            ))) {
                return false;
            }
        }
    }
    return true;
}

function blindbox_transactions_ready()
{
    global $wpdb;
    foreach (array(
        $wpdb->usermeta, blindbox_daily_table(), blindbox_operation_table(),
    ) as $table) {
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

function blindbox_integer($value, $min, $max)
{
    if (is_int($value)) {
        $text = (string) $value;
    } elseif (is_string($value)) {
        $text = trim($value);
    } else {
        return null;
    }
    if (!preg_match('/\A[0-9]+\z/', $text)) {
        return null;
    }
    $number = (int) $text;
    return $number >= $min && $number <= $max ? $number : null;
}

function blindbox_probability($value)
{
    if (is_int($value)) {
        $text = (string) $value;
    } elseif (is_string($value)) {
        $text = trim($value);
    } else {
        return null;
    }
    if (!preg_match('/\A(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?\z/', $text)) {
        return null;
    }
    $parts = explode('.', $text, 2);
    $whole = (int) $parts[0];
    $fraction = str_pad($parts[1] ?? '', 2, '0');
    $basis = $whole * 100 + (int) $fraction;
    if ($basis < 0 || $basis > 10000) {
        return null;
    }
    return array(
        'bp' => $basis,
        'display' => number_format($basis / 100, 2, '.', ''),
    );
}

function blindbox_validate_config($raw)
{
    if (!is_array($raw)) {
        return failure('invalid_parameters', 400);
    }
    $free = blindbox_integer($raw['daily_free_limit'] ?? null, 0, BLINDBOX_MAX_LIMIT);
    $exchange = blindbox_integer($raw['daily_exchange_limit'] ?? null, 0, BLINDBOX_MAX_LIMIT);
    $cost = blindbox_integer($raw['exchange_cost_points'] ?? null, 1, BLINDBOX_MAX_POINTS);
    if ($free === null || $exchange === null || $cost === null) {
        return failure('blindbox_config_invalid', 400);
    }
    $raw_prizes = $raw['prizes'] ?? null;
    if (!is_array($raw_prizes) || count($raw_prizes) < 1 || count($raw_prizes) > BLINDBOX_MAX_PRIZES) {
        return failure('blindbox_config_invalid', 400);
    }
    $prizes = array();
    $ids = array();
    $total = 0;
    foreach (array_values($raw_prizes) as $raw_prize) {
        if (!is_array($raw_prize)) {
            return failure('blindbox_config_invalid', 400);
        }
        $id = $raw_prize['id'] ?? null;
        $name = $raw_prize['name'] ?? null;
        $type = $raw_prize['type'] ?? null;
        if (!is_string($id) || preg_match('/\A[^\s\p{C}|]{1,64}\z/u', $id) !== 1
            || isset($ids[$id])
            || !is_string($name) || preg_match('/\A[^\p{C}]{1,40}\z/u', $name) !== 1
            || !is_string($type) || !in_array($type, array('points', 'text'), true)
        ) {
            return failure('blindbox_config_invalid', 400);
        }
        $probability = blindbox_probability($raw_prize['probability'] ?? null);
        if (!$probability) {
            return failure('blindbox_config_invalid', 400);
        }
        $ids[$id] = true;
        $item = array(
            'id' => $id,
            'name' => $name,
            'type' => $type,
            'probability' => $probability['display'],
            'probability_bp' => $probability['bp'],
        );
        if ($type === 'points') {
            $min = blindbox_integer($raw_prize['points_min'] ?? null, 1, BLINDBOX_MAX_POINTS);
            $max = blindbox_integer($raw_prize['points_max'] ?? null, 1, BLINDBOX_MAX_POINTS);
            if ($min === null || $max === null || $min > $max) {
                return failure('blindbox_config_invalid', 400);
            }
            $item['points_min'] = $min;
            $item['points_max'] = $max;
        } else {
            $text = $raw_prize['text'] ?? null;
            if (!is_string($text) || preg_match('/\A[^\p{C}]{1,500}\z/u', $text) !== 1) {
                return failure('blindbox_config_invalid', 400);
            }
            $item['text'] = $text;
        }
        $total += $probability['bp'];
        $prizes[] = $item;
    }
    if ($total !== 10000) {
        return failure('blindbox_config_invalid', 400);
    }
    return array(
        'enabled' => ($raw['enabled'] ?? false) === true || ($raw['enabled'] ?? '') === '1',
        'daily_free_limit' => $free,
        'daily_exchange_limit' => $exchange,
        'exchange_cost_points' => $cost,
        'prizes' => $prizes,
    );
}

function blindbox_runtime_config()
{
    $config = settings()['blindbox'] ?? null;
    $valid = blindbox_validate_config($config);
    return is_wp_error($valid) ? $valid : $valid;
}

function blindbox_request_key($actor_hash, $request_id)
{
    return hash('sha256', 'blindbox:' . $actor_hash . ':' . $request_id);
}

function blindbox_input_hash($action, $actor, $event_time)
{
    return hash('sha256', wp_json_encode(array(
        'action' => $action,
        'actor' => $actor['actor_hash'],
        'event_time' => $event_time,
    ), JSON_UNESCAPED_UNICODE));
}

function blindbox_operation_row($request_key)
{
    global $wpdb;
    return $wpdb->get_row($wpdb->prepare(
        'SELECT * FROM ' . blindbox_operation_table() . ' WHERE request_key = %s',
        $request_key
    ), ARRAY_A);
}

function blindbox_daily_row($uid, $day, $for_update = false)
{
    global $wpdb;
    $sql = 'SELECT * FROM ' . blindbox_daily_table()
        . ' WHERE user_id = %d AND site_day = %s';
    if ($for_update) {
        $sql .= ' FOR UPDATE';
    }
    return $wpdb->get_row($wpdb->prepare($sql, $uid, $day), ARRAY_A);
}

function blindbox_ensure_daily_row($uid, $day)
{
    global $wpdb;
    $table = blindbox_daily_table();
    $inserted = $wpdb->query($wpdb->prepare(
        "INSERT IGNORE INTO $table
         (user_id,site_day,free_used,exchange_bought,exchange_used,updated_at)
         VALUES (%d,%s,0,0,0,%d)",
        $uid, $day, time()
    ));
    if ($inserted === false) {
        return false;
    }
    return blindbox_daily_row($uid, $day, true);
}

function blindbox_daily_snapshot($row, $config)
{
    $free_used = (int) ($row['free_used'] ?? 0);
    $exchange_bought = (int) ($row['exchange_bought'] ?? 0);
    $exchange_used = (int) ($row['exchange_used'] ?? 0);
    $free_remaining = max(0, (int) $config['daily_free_limit'] - $free_used);
    $exchange_remaining = max(0, $exchange_bought - $exchange_used);
    return array(
        'free_used' => $free_used,
        'exchange_bought' => $exchange_bought,
        'exchange_used' => $exchange_used,
        'free_remaining' => $free_remaining,
        'exchange_remaining' => $exchange_remaining,
        'available' => $free_remaining + $exchange_remaining,
        'daily_free_limit' => (int) $config['daily_free_limit'],
        'exchange_limit' => (int) $config['daily_exchange_limit'],
        'exchange_available' => max(0, (int) $config['daily_exchange_limit'] - $exchange_bought),
        'exchange_cost_points' => (int) $config['exchange_cost_points'],
    );
}

function blindbox_prize_snapshot($prize, $points)
{
    $result = array(
        'id' => $prize['id'],
        'name' => $prize['name'],
        'type' => $prize['type'],
    );
    if ($prize['type'] === 'points') {
        $result['points'] = (int) $points;
    } else {
        $result['text'] = $prize['text'];
    }
    return $result;
}

function blindbox_pick_prize($config)
{
    try {
        $draw = random_int(1, 10000);
    } catch (\Throwable $error) {
        return failure('blindbox_random_failed', 503);
    }
    $cursor = 0;
    foreach ($config['prizes'] as $prize) {
        $cursor += (int) $prize['probability_bp'];
        if ($draw <= $cursor) {
            $points = 0;
            if ($prize['type'] === 'points') {
                try {
                    $points = random_int((int) $prize['points_min'], (int) $prize['points_max']);
                } catch (\Throwable $error) {
                    return failure('blindbox_random_failed', 503);
                }
            }
            return array(
                'prize' => blindbox_prize_snapshot($prize, $points),
                'points' => $points,
            );
        }
    }
    return failure('blindbox_random_failed', 503);
}

function blindbox_response($row)
{
    $data = is_string($row['result_json'] ?? null)
        ? json_decode($row['result_json'], true) : null;
    if (!is_array($data)) {
        $data = array();
    }
    $state = $row['state'] === 'pending' ? 'uncertain' : $row['state'];
    $data['state'] = $state;
    $data['request_id'] = $row['request_id'];
    $data['action'] = $row['action'];
    $data['uid'] = (int) $row['user_id'];
    if (!array_key_exists('site_day', $data)) {
        $data['site_day'] = $row['site_day'];
    }
    if (!array_key_exists('timezone', $data)) {
        $data['timezone'] = $row['timezone'];
    }
    if ($row['error_code'] !== '') {
        $data['error_code'] = $row['error_code'];
    }
    return success($data);
}

function blindbox_finish($request_key, $state, $error_code, $data)
{
    global $wpdb;
    $updated = blindbox_store_terminal($request_key, $state, $error_code, $data);
    if ($updated === false) {
        return failure('unavailable', 503);
    }
    $row = blindbox_operation_row($request_key);
    return $row ? blindbox_response($row) : failure('unavailable', 503);
}

function blindbox_store_terminal($request_key, $state, $error_code, $data)
{
    global $wpdb;
    return $wpdb->update(blindbox_operation_table(), array(
        'state' => $state,
        'error_code' => $error_code,
        'result_json' => wp_json_encode($data, JSON_UNESCAPED_UNICODE),
        'updated_at' => time(),
    ), array('request_key' => $request_key));
}

function blindbox_committed_response($request_key)
{
    $row = blindbox_operation_row($request_key);
    return $row ? blindbox_response($row) : failure('unavailable', 503);
}

function blindbox_connection_matches($connection_id)
{
    global $wpdb;
    return $connection_id !== ''
        && (string) $wpdb->get_var('SELECT CONNECTION_ID()') === $connection_id;
}

function blindbox_commit($request_key, $uid, $connection_id)
{
    global $wpdb;
    if (blindbox_connection_matches($connection_id)
        && $wpdb->query('COMMIT') !== false
        && blindbox_connection_matches($connection_id)
    ) {
        return true;
    }
    blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    return false;
}

function blindbox_abort($request_key, $uid, $state, $error_code, $data)
{
    global $wpdb;
    if ($wpdb->query('ROLLBACK') === false) {
        $wpdb->close();
        fresh_user_meta($uid);
        return failure('uncertain', 503);
    }
    fresh_user_meta($uid);
    return blindbox_finish($request_key, $state, $error_code, $data);
}

function blindbox_validate_event($input)
{
    $request_id = $input['request_id'] ?? null;
    $event_time = $input['event_time'] ?? null;
    if (!is_string($request_id) || !preg_match('/\A[0-9a-f]{64}\z/', $request_id)
        || !is_int($event_time)
    ) {
        return failure('event_identity_required', 400);
    }
    return array('request_id' => $request_id, 'event_time' => $event_time);
}

function blindbox_base_data($uid, $day, $timezone, $snapshot)
{
    return array_merge(array(
        'site_day' => $day,
        'timezone' => $timezone,
    ), $snapshot);
}

function blindbox_create_operation($request_key, $event, $actor, $uid, $day, $timezone, $action, $config, $input_hash)
{
    global $wpdb;
    $now = time();
    $saved = $wpdb->insert(blindbox_operation_table(), array(
        'request_key' => $request_key,
        'request_id' => $event['request_id'],
        'input_hash' => $input_hash,
        'actor_hash' => $actor['actor_hash'],
        'user_id' => $uid,
        'site_day' => $day,
        'timezone' => $timezone,
        'action' => $action,
        'rule_hash' => hash('sha256', wp_json_encode($config, JSON_UNESCAPED_UNICODE)),
        'rule_json' => wp_json_encode($config, JSON_UNESCAPED_UNICODE),
        'state' => 'pending',
        'created_at' => $now,
        'updated_at' => $now,
        'expires_at' => $now + 7 * DAY_IN_SECONDS,
    ));
    return $saved === 1;
}

function blindbox_user_binding_matches($actor_hash, $uid)
{
    $row = binding_row($actor_hash);
    return $row && $row['user_id'] && (int) $row['user_id'] === (int) $uid;
}

function blindbox_unresolved_operation($uid)
{
    global $wpdb;
    return $wpdb->get_var($wpdb->prepare(
        'SELECT request_id FROM ' . blindbox_operation_table()
        . " WHERE user_id = %d AND state IN ('pending','uncertain')"
        . ' ORDER BY created_at ASC LIMIT 1',
        $uid
    ));
}

function blindbox_operation_for_actor($actor, $request_id)
{
    return blindbox_operation_row(blindbox_request_key($actor['actor_hash'], $request_id));
}

function blindbox_status($actor, $input)
{
    $request_id = $input['request_id'] ?? null;
    if (!is_string($request_id) || !preg_match('/\A[0-9a-f]{64}\z/', $request_id)) {
        return failure('event_identity_required', 400);
    }
    return with_lock('actor:' . $actor['actor_hash'], static function () use ($actor, $request_id) {
        $user = bound_user($actor['actor_hash']);
        if (is_wp_error($user)) {
            return $user;
        }
        $uid = (int) $user->ID;
        return with_lock('checkin:' . $uid, static function () use ($actor, $request_id, $uid) {
            $row = blindbox_operation_row(blindbox_request_key($actor['actor_hash'], $request_id));
            if ($row) {
                if ((int) $row['user_id'] !== $uid || !blindbox_user_binding_matches($actor['actor_hash'], $uid)) {
                    return failure('binding_changed', 409);
                }
                return blindbox_response($row);
            }
            return success(array('state' => 'not_started', 'request_id' => $request_id));
        });
    });
}

function blindbox_open_transaction($actor, $uid, $event, $request_key, $input_hash, $config, $day, $timezone)
{
    global $wpdb;
    $existing = blindbox_operation_row($request_key);
    if ($existing) {
        if (!hash_equals($existing['input_hash'], $input_hash)) {
            return failure('request_conflict', 409);
        }
        if ((int) $existing['user_id'] !== $uid || !blindbox_user_binding_matches($actor['actor_hash'], $uid)) {
            return failure('binding_changed', 409);
        }
        return blindbox_response($existing);
    }
    if (blindbox_unresolved_operation($uid)) {
        return failure('blindbox_unresolved', 409);
    }
    if (!blindbox_create_operation(
        $request_key, $event, $actor, $uid, $day, $timezone, 'open', $config, $input_hash
    )) {
        $existing = blindbox_operation_row($request_key);
        return $existing && hash_equals($existing['input_hash'], $input_hash)
            && (int) $existing['user_id'] === $uid
            && blindbox_user_binding_matches($actor['actor_hash'], $uid)
            ? blindbox_response($existing) : failure('unavailable', 503);
    }
    $connection_id = (string) $wpdb->get_var('SELECT CONNECTION_ID()');
    if ($connection_id === '' || $wpdb->query('START TRANSACTION') === false) {
        return blindbox_finish($request_key, 'uncertain', 'blindbox_transaction_failed', array());
    }
    if (!blindbox_connection_matches($connection_id)) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    }
    $daily = blindbox_ensure_daily_row($uid, $day);
    if (!is_array($daily) || !blindbox_connection_matches($connection_id)) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    }
    $snapshot_before = blindbox_daily_snapshot($daily, $config);
    if ($snapshot_before['available'] < 1) {
        $data = blindbox_base_data($uid, $day, $timezone, $snapshot_before);
        if ($wpdb->update(blindbox_operation_table(), array(
            'free_used_before' => $snapshot_before['free_used'],
            'free_used_after' => $snapshot_before['free_used'],
            'exchange_bought_before' => $snapshot_before['exchange_bought'],
            'exchange_bought_after' => $snapshot_before['exchange_bought'],
            'exchange_used_before' => $snapshot_before['exchange_used'],
            'exchange_used_after' => $snapshot_before['exchange_used'],
            'points_before' => (int) zibpay_get_user_points($uid),
            'points_after' => (int) zibpay_get_user_points($uid),
            'updated_at' => time(),
        ), array('request_key' => $request_key)) === false || !blindbox_connection_matches($connection_id)) {
            return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
        }
        if (blindbox_store_terminal($request_key, 'rejected', 'blindbox_no_chances', $data) === false
            || !blindbox_connection_matches($connection_id)
        ) {
            return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
        }
        if (!blindbox_commit($request_key, $uid, $connection_id)) {
            return failure('uncertain', 503);
        }
        return blindbox_committed_response($request_key);
    }
    if (!points_lock_user_meta($uid) || !blindbox_connection_matches($connection_id)) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    }
    fresh_user_meta($uid);
    $points_before = (int) zibpay_get_user_points($uid);
    if (!blindbox_connection_matches($connection_id)) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    }
    $source = $snapshot_before['free_remaining'] > 0 ? 'free' : 'exchange';
    $draw = blindbox_pick_prize($config);
    if (is_wp_error($draw)) {
        return blindbox_abort($request_key, $uid, 'rejected', $draw->get_error_code(), blindbox_base_data(
            $uid, $day, $timezone, $snapshot_before
        ));
    }
    $daily_after = array(
        'free_used' => $snapshot_before['free_used'] + ($source === 'free' ? 1 : 0),
        'exchange_bought' => $snapshot_before['exchange_bought'],
        'exchange_used' => $snapshot_before['exchange_used'] + ($source === 'exchange' ? 1 : 0),
    );
    $changed = $wpdb->update(blindbox_daily_table(), array(
        'free_used' => $daily_after['free_used'],
        'exchange_bought' => $daily_after['exchange_bought'],
        'exchange_used' => $daily_after['exchange_used'],
        'updated_at' => time(),
    ), array('user_id' => $uid, 'site_day' => $day));
    if ($changed === false || !blindbox_connection_matches($connection_id)) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    }
    $points_delta = (int) $draw['points'];
    $points_after = $points_before;
    if ($points_delta > 0) {
        $order_num = 'astrbot:blindbox:' . substr($request_key, 0, 24);
        if (points_has_order_num($uid, $order_num)) {
            return blindbox_abort($request_key, $uid, 'uncertain', 'points_record_conflict', array());
        }
        if (!blindbox_connection_matches($connection_id)) {
            return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
        }
        try {
            $updated = zibpay_update_user_points($uid, array(
                'value' => $points_delta,
                'type' => 'AstrBot 盲盒奖励',
                'desc' => 'AstrBot 盲盒奖励：' . $draw['prize']['name'],
                'order_num' => $order_num,
            ));
        } catch (\Throwable $error) {
            $updated = false;
        }
        if (!blindbox_connection_matches($connection_id)) {
            return blindbox_abort($request_key, $uid, 'uncertain', 'points_update_uncertain', array());
        }
        fresh_user_meta($uid);
        $points_after = (int) zibpay_get_user_points($uid);
        if ($updated !== true || $wpdb->last_error !== ''
            || $points_after !== $points_before + $points_delta
            || !points_record_matches($uid, $order_num, $points_delta, $points_after)
            || !blindbox_connection_matches($connection_id)
        ) {
            return blindbox_abort($request_key, $uid, 'uncertain', 'points_update_uncertain', array());
        }
    }
    $daily_after_row = array_merge($daily, $daily_after);
    $snapshot_after = blindbox_daily_snapshot($daily_after_row, $config);
    $data = blindbox_base_data($uid, $day, $timezone, $snapshot_after);
    $data['prize'] = $draw['prize'];
    $data['points_before'] = $points_before;
    $data['points_delta'] = $points_delta;
    $data['points_after'] = $points_after;
    $data['use_source'] = $source;
    if ($wpdb->update(blindbox_operation_table(), array(
        'prize_json' => wp_json_encode($draw['prize'], JSON_UNESCAPED_UNICODE),
        'use_source' => $source,
        'points_before' => $points_before,
        'points_delta' => $points_delta,
        'points_after' => $points_after,
        'free_used_before' => $snapshot_before['free_used'],
        'free_used_after' => $snapshot_after['free_used'],
        'exchange_bought_before' => $snapshot_before['exchange_bought'],
        'exchange_bought_after' => $snapshot_after['exchange_bought'],
        'exchange_used_before' => $snapshot_before['exchange_used'],
        'exchange_used_after' => $snapshot_after['exchange_used'],
        'updated_at' => time(),
    ), array('request_key' => $request_key)) === false || !blindbox_connection_matches($connection_id)) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    }
    if (blindbox_store_terminal($request_key, 'completed', '', $data) === false
        || !blindbox_connection_matches($connection_id)
    ) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    }
    if (!blindbox_commit($request_key, $uid, $connection_id)) {
        return failure('uncertain', 503);
    }
    fresh_user_meta($uid);
    return blindbox_committed_response($request_key);
}

function blindbox_exchange_transaction($actor, $uid, $event, $request_key, $input_hash, $config, $day, $timezone)
{
    global $wpdb;
    $existing = blindbox_operation_row($request_key);
    if ($existing) {
        if (!hash_equals($existing['input_hash'], $input_hash)) {
            return failure('request_conflict', 409);
        }
        if ((int) $existing['user_id'] !== $uid || !blindbox_user_binding_matches($actor['actor_hash'], $uid)) {
            return failure('binding_changed', 409);
        }
        return blindbox_response($existing);
    }
    if (blindbox_unresolved_operation($uid)) {
        return failure('blindbox_unresolved', 409);
    }
    if (!blindbox_create_operation(
        $request_key, $event, $actor, $uid, $day, $timezone, 'exchange', $config, $input_hash
    )) {
        $existing = blindbox_operation_row($request_key);
        return $existing && hash_equals($existing['input_hash'], $input_hash)
            && (int) $existing['user_id'] === $uid
            && blindbox_user_binding_matches($actor['actor_hash'], $uid)
            ? blindbox_response($existing) : failure('unavailable', 503);
    }
    $connection_id = (string) $wpdb->get_var('SELECT CONNECTION_ID()');
    if ($connection_id === '' || $wpdb->query('START TRANSACTION') === false) {
        return blindbox_finish($request_key, 'uncertain', 'blindbox_transaction_failed', array());
    }
    if (!blindbox_connection_matches($connection_id)) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    }
    $daily = blindbox_ensure_daily_row($uid, $day);
    if (!is_array($daily) || !blindbox_connection_matches($connection_id)) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    }
    $snapshot_before = blindbox_daily_snapshot($daily, $config);
    $points_before = (int) zibpay_get_user_points($uid);
    if (!blindbox_connection_matches($connection_id)) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    }
    $base = blindbox_base_data($uid, $day, $timezone, $snapshot_before);
    if ($snapshot_before['exchange_bought'] >= (int) $config['daily_exchange_limit']) {
        $updated = $wpdb->update(blindbox_operation_table(), array(
            'points_before' => $points_before, 'points_after' => $points_before,
            'free_used_before' => $snapshot_before['free_used'],
            'free_used_after' => $snapshot_before['free_used'],
            'exchange_bought_before' => $snapshot_before['exchange_bought'],
            'exchange_bought_after' => $snapshot_before['exchange_bought'],
            'exchange_used_before' => $snapshot_before['exchange_used'],
            'exchange_used_after' => $snapshot_before['exchange_used'],
            'updated_at' => time(),
        ), array('request_key' => $request_key));
        if ($updated === false || !blindbox_connection_matches($connection_id)
            || blindbox_store_terminal($request_key, 'rejected', 'blindbox_exchange_limit', $base) === false
            || !blindbox_connection_matches($connection_id)
        ) {
            return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
        }
        if (!blindbox_commit($request_key, $uid, $connection_id)) {
            return failure('uncertain', 503);
        }
        return blindbox_committed_response($request_key);
    }
    if (!points_lock_user_meta($uid) || !blindbox_connection_matches($connection_id)) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    }
    fresh_user_meta($uid);
    $points_before = (int) zibpay_get_user_points($uid);
    if (!blindbox_connection_matches($connection_id)) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    }
    $cost = (int) $config['exchange_cost_points'];
    if ($points_before < $cost) {
        $base['points_before'] = $points_before;
        $base['points_after'] = $points_before;
        $base['cost_points'] = $cost;
        $updated = $wpdb->update(blindbox_operation_table(), array(
            'points_before' => $points_before, 'points_after' => $points_before,
            'updated_at' => time(),
        ), array('request_key' => $request_key));
        if ($updated === false || !blindbox_connection_matches($connection_id)
            || blindbox_store_terminal($request_key, 'rejected', 'insufficient_points', $base) === false
            || !blindbox_connection_matches($connection_id)
        ) {
            return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
        }
        if (!blindbox_commit($request_key, $uid, $connection_id)) {
            return failure('uncertain', 503);
        }
        return blindbox_committed_response($request_key);
    }
    $order_num = 'astrbot:blindbox:' . substr($request_key, 0, 24);
    if (points_has_order_num($uid, $order_num)) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'points_record_conflict', array());
    }
    if (!blindbox_connection_matches($connection_id)) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    }
    try {
        $updated = zibpay_update_user_points($uid, array(
            'value' => -$cost,
            'type' => 'AstrBot 兑换盲盒次数',
            'desc' => 'AstrBot 兑换 1 次盲盒机会',
            'order_num' => $order_num,
        ));
    } catch (\Throwable $error) {
        $updated = false;
    }
    if (!blindbox_connection_matches($connection_id)) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'points_update_uncertain', array());
    }
    fresh_user_meta($uid);
    $points_after = (int) zibpay_get_user_points($uid);
    if ($updated !== true || $wpdb->last_error !== ''
        || $points_after !== $points_before - $cost
        || !points_record_matches($uid, $order_num, -$cost, $points_after)
        || !blindbox_connection_matches($connection_id)
    ) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'points_update_uncertain', array());
    }
    $daily_after = array(
        'free_used' => (int) $daily['free_used'],
        'exchange_bought' => (int) $daily['exchange_bought'] + 1,
        'exchange_used' => (int) $daily['exchange_used'],
    );
    if ($wpdb->update(blindbox_daily_table(), array(
        'exchange_bought' => $daily_after['exchange_bought'],
        'updated_at' => time(),
    ), array('user_id' => $uid, 'site_day' => $day)) === false || !blindbox_connection_matches($connection_id)) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    }
    $snapshot_after = blindbox_daily_snapshot(array_merge($daily, $daily_after), $config);
    $data = blindbox_base_data($uid, $day, $timezone, $snapshot_after);
    $data['cost_points'] = $cost;
    $data['points_before'] = $points_before;
    $data['points_delta'] = -$cost;
    $data['points_after'] = $points_after;
    if ($wpdb->update(blindbox_operation_table(), array(
        'points_before' => $points_before,
        'points_delta' => -$cost,
        'points_after' => $points_after,
        'free_used_before' => $snapshot_before['free_used'],
        'free_used_after' => $snapshot_after['free_used'],
        'exchange_bought_before' => $snapshot_before['exchange_bought'],
        'exchange_bought_after' => $snapshot_after['exchange_bought'],
        'exchange_used_before' => $snapshot_before['exchange_used'],
        'exchange_used_after' => $snapshot_after['exchange_used'],
        'updated_at' => time(),
    ), array('request_key' => $request_key)) === false || !blindbox_connection_matches($connection_id)) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    }
    if (blindbox_store_terminal($request_key, 'completed', '', $data) === false
        || !blindbox_connection_matches($connection_id)
    ) {
        return blindbox_abort($request_key, $uid, 'uncertain', 'blindbox_transaction_failed', array());
    }
    if (!blindbox_commit($request_key, $uid, $connection_id)) {
        return failure('uncertain', 503);
    }
    fresh_user_meta($uid);
    return blindbox_committed_response($request_key);
}

function blindbox_action($action, $actor, $input)
{
    if (!in_array($action, array('blindbox/open', 'blindbox/exchange', 'blindbox/status'), true)) {
        return failure('invalid_parameters', 400);
    }
    if (!blindbox_schema_ready()) {
        return failure('unavailable', 503);
    }
    if ($action === 'blindbox/status') {
        return blindbox_status($actor, $input);
    }
    $event = blindbox_validate_event($input);
    if (is_wp_error($event)) {
        return $event;
    }
    $request_key = blindbox_request_key($actor['actor_hash'], $event['request_id']);
    $input_hash = blindbox_input_hash($action, $actor, $event['event_time']);
    return with_lock('actor:' . $actor['actor_hash'], static function () use (
        $action, $actor, $event, $request_key, $input_hash
    ) {
        $user = bound_user($actor['actor_hash']);
        if (is_wp_error($user)) {
            return $user;
        }
        $uid = (int) $user->ID;
        return with_lock('checkin:' . $uid, static function () use (
            $action, $actor, $uid, $event, $request_key, $input_hash
        ) {
            $existing = blindbox_operation_row($request_key);
            if ($existing) {
                if (!hash_equals($existing['input_hash'], $input_hash)) {
                    return failure('request_conflict', 409);
                }
                if ((int) $existing['user_id'] !== $uid
                    || !blindbox_user_binding_matches($actor['actor_hash'], $uid)
                ) {
                    return failure('binding_changed', 409);
                }
                return blindbox_response($existing);
            }
            if (abs(time() - $event['event_time']) > CLOCK_WINDOW) {
                return failure('event_expired', 409);
            }
            $config = blindbox_runtime_config();
            if (is_wp_error($config)) {
                return $config;
            }
            if (!$config['enabled']) {
                return failure('blindbox_disabled', 403);
            }
            if (!blindbox_transactions_ready() || !_pz('points_s')) {
                return failure('unavailable', 503);
            }
            $day = current_datetime()->format('Y-m-d');
            $timezone = wp_timezone_string() ?: 'UTC';
            return $action === 'blindbox/open'
                ? blindbox_open_transaction($actor, $uid, $event, $request_key, $input_hash, $config, $day, $timezone)
                : blindbox_exchange_transaction($actor, $uid, $event, $request_key, $input_hash, $config, $day, $timezone);
        });
    });
}
