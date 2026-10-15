import Foundation
import Security

private let service = "com.claimflow.desktop.openai"
private let account = "openai-api-key"

struct Request: Decodable {
    let operation: String
    let value: String?
}

struct Response: Encodable {
    let status: String
    let value: String?
    let code: String?
}

func writeResponse(_ response: Response) {
    let encoder = JSONEncoder()
    guard let data = try? encoder.encode(response), let text = String(data: data, encoding: .utf8) else {
        return
    }
    print(text)
}

func itemQuery() -> [String: Any] {
    [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: service,
        kSecAttrAccount as String: account,
    ]
}

func readValue() -> Response {
    var query = itemQuery()
    query[kSecReturnData as String] = true
    query[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    if status == errSecItemNotFound {
        return Response(status: "missing", value: nil, code: nil)
    }
    guard status == errSecSuccess,
          let data = result as? Data,
          let value = String(data: data, encoding: .utf8),
          !value.isEmpty else {
        return Response(status: "error", value: nil, code: "keychain_unavailable")
    }
    return Response(status: "found", value: value, code: nil)
}

func writeValue(_ rawValue: String?) -> Response {
    guard let rawValue else {
        return Response(status: "error", value: nil, code: "invalid_value")
    }
    let value = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !value.isEmpty, value.utf8.count <= 1024 else {
        return Response(status: "error", value: nil, code: "invalid_value")
    }
    let data = Data(value.utf8)
    let updateStatus = SecItemUpdate(
        itemQuery() as CFDictionary,
        [kSecValueData as String: data] as CFDictionary
    )
    if updateStatus == errSecSuccess {
        return Response(status: "ok", value: nil, code: nil)
    }
    guard updateStatus == errSecItemNotFound else {
        return Response(status: "error", value: nil, code: "keychain_unavailable")
    }
    var attributes = itemQuery()
    attributes[kSecValueData as String] = data
    attributes[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
    let addStatus = SecItemAdd(attributes as CFDictionary, nil)
    return addStatus == errSecSuccess
        ? Response(status: "ok", value: nil, code: nil)
        : Response(status: "error", value: nil, code: "keychain_unavailable")
}

func removeValue() -> Response {
    let status = SecItemDelete(itemQuery() as CFDictionary)
    if status == errSecSuccess || status == errSecItemNotFound {
        return Response(status: "ok", value: nil, code: nil)
    }
    return Response(status: "error", value: nil, code: "keychain_unavailable")
}

let input = FileHandle.standardInput.readDataToEndOfFile()
guard let request = try? JSONDecoder().decode(Request.self, from: input) else {
    writeResponse(Response(status: "error", value: nil, code: "invalid_request"))
    exit(0)
}

switch request.operation {
case "read": writeResponse(readValue())
case "write": writeResponse(writeValue(request.value))
case "remove": writeResponse(removeValue())
default: writeResponse(Response(status: "error", value: nil, code: "invalid_request"))
}
