alias SymphonyElixir.Linear.Issue

node_name = System.fetch_env!("SYMPHONY_DAEMON_NODE")
cookie = System.fetch_env!("SYMPHONY_DAEMON_COOKIE")
issues_json_path = System.fetch_env!("TASK_ISSUES_JSON")

sender_node = String.to_atom("enqueue_#{System.unique_integer([:positive])}")
{:ok, _} = Node.start(sender_node, :shortnames)
Node.set_cookie(String.to_atom(cookie))

resolved_node_name =
  if String.contains?(node_name, "@") do
    node_name
  else
    {:ok, host} = :inet.gethostname()

    host_short =
      host
      |> to_string()
      |> String.split(".", parts: 2)
      |> hd()

    "#{node_name}@#{host_short}"
  end

target_node = String.to_atom(resolved_node_name)

connect_ok? =
  1..20
  |> Enum.any?(fn _attempt ->
    if Node.connect(target_node) do
      true
    else
      Process.sleep(250)
      false
    end
  end)

if not connect_ok? do
  IO.puts(:stderr, "failed_to_connect node=#{resolved_node_name}")
  System.halt(1)
end

issues_payload =
  issues_json_path
  |> File.read!()
  |> Jason.decode!()

incoming_issues =
  Enum.map(issues_payload["issues"], fn item ->
    assigned_to_worker =
      case Map.fetch(item, "assigned_to_worker") do
        {:ok, value} when is_boolean(value) -> value
        {:ok, _value} -> true
        :error -> true
      end

    %Issue{
      id: item["id"],
      identifier: item["identifier"],
      title: item["title"],
      state: item["state"] || "Todo",
      description: item["description"] || "",
      labels: item["labels"] || [],
      assigned_to_worker: assigned_to_worker
    }
  end)

existing_issues =
  case :rpc.call(target_node, Application, :get_env, [:symphony_elixir, :memory_tracker_issues, []]) do
    issues when is_list(issues) -> Enum.filter(issues, &match?(%Issue{}, &1))
    _ -> []
  end

existing_tagged = Enum.map(existing_issues, &{:existing, &1})
incoming_tagged = Enum.map(incoming_issues, &{:incoming, &1})

merged_by_id =
  (existing_tagged ++ incoming_tagged)
  |> Enum.reduce(%{}, fn {source, issue}, acc ->
    case Map.get(acc, issue.id) do
      nil ->
        :ok

      {prev_source, _prev_issue} ->
        IO.puts(
          :stderr,
          "overwriting_issue id=#{issue.id} previous_source=#{prev_source} new_source=#{source}"
        )
    end

    Map.put(acc, issue.id, {source, issue})
  end)

merged_issues =
  merged_by_id
  |> Map.values()
  |> Enum.map(fn {_source, issue} -> issue end)

:ok =
  :rpc.call(target_node, Application, :put_env, [
    :symphony_elixir,
    :memory_tracker_issues,
    merged_issues
  ])

IO.puts("enqueued=#{length(incoming_issues)}")
IO.puts("total_queue=#{length(merged_issues)}")

for issue <- incoming_issues do
  IO.puts("issue=#{issue.identifier}\ttitle=#{issue.title}")
end
